#!/usr/bin/python
# -*- coding: utf-8 -*-
import os
import logging
import shutil

from container import Container
import misc
import defaults
import json
import glob
from dockerutils import (exec_dockerps,
                         get_docker_container_json_logs_path,
                         get_docker_container_rootfs_path,
                         exec_dockerinspect)
import plugins_manager
from crawler_exceptions import (ContainerInvalidEnvironment,
                                ContainerNonExistent,
                                DockerutilsNoJsonLog,
                                DockerutilsException,
                                ContainerWithoutCgroups)
from requests.exceptions import HTTPError

logger = logging.getLogger('crawlutils')


def list_docker_containers(container_opts={}):
    """
    Get the list of running Docker containers, as `DockerContainer` objects.

    This is basically polling. Ideally, we should subscribe to Docker
    events so we can keep the containers list up to date without having to
    poll like this.
    """
    for inspect in exec_dockerps():
        long_id = inspect['Id']
        try:
            c = DockerContainer(long_id, inspect, container_opts)
            if c.namespace:
                yield c
        except ContainerInvalidEnvironment as e:
            logger.exception(e)


class LogFileLink():
    """
    If `host_log_dir is not None`, then we should prefix `dest` with
    `host_log_dir`.
    """
    def __init__(self, name=None, type=None, source=None,
                 dest=None, host_log_dir=None):
        self.name = name
        self.type = type
        self.source = source
        self.dest = dest
        self.host_log_dir = host_log_dir

    def __str__(self):
            return "%s: %s --> %s" % (self.name, self.source, self.dest)

    def get_dest(self):
        if self.host_log_dir:
            return misc.join_abs_paths(self.host_log_dir, self.dest)
        return self.dest


class DockerContainer(Container):

    DOCKER_JSON_LOG_FILE = "docker.log"

    def __init__(
        self,
        long_id,
        inspect=None,
        container_opts={},
    ):

        # Some quick sanity checks
        if not isinstance(long_id, basestring):
            raise TypeError('long_id should be a string')
        if inspect and not isinstance(inspect, dict):
            raise TypeError('inspect should be a dict.')
        if container_opts and not isinstance(container_opts, dict):
            raise TypeError('container_opts should be a dict.')

        if not inspect:
            try:
                inspect = exec_dockerinspect(long_id)
            except HTTPError:
                raise ContainerNonExistent('No docker container with ID: %s'
                                           % long_id)

        state = inspect['State']
        self.image = inspect['Image']

        assert(long_id == inspect['Id'])
        self.long_id = long_id
        self.pid = str(state['Pid'])
        self.name = inspect['Name']
        self.running = state['Running']
        self.created = inspect['Created']
        self.network_settings = inspect['NetworkSettings']
        self.cmd = inspect['Config']['Cmd']
        self.mounts = inspect.get('Mounts')
        self.volumes = inspect.get('Volumes')
        self.inspect = inspect

        # This short ID is mainly used for logging purposes
        self.short_id = long_id[:12]

        # Docker prepends a '/' to the name. Let's remove it.
        if self.name[0] == '/':
            self.name = self.name[1:]

        self._set_image_fields(inspect.get('RepoTag', ''))
        self._set_mounts_list()

        try:
            self.root_fs = get_docker_container_rootfs_path(self.long_id)
        except (HTTPError, RuntimeError, DockerutilsException) as e:
            logger.exception(e)
            self.root_fs = None

        self._set_logs_list_input()
        self._set_environment_specific_options(container_opts)
        self._set_logs_list()

    def _set_image_fields(self, repo_tag):
        """
        This function parses the image repository:tag string to try
        to get info like the registry, and the "owner_namespace".
        This "owner_namespace" field is not exactly officially a docker
        concept, but it usually points to the owner of the image.
        """
        self.docker_image_long_name = repo_tag
        self.docker_image_short_name = os.path.basename(repo_tag)
        if (':' in repo_tag) and ('/' not in repo_tag.rsplit(':', 1)[1]):
            self.docker_image_tag = repo_tag.rsplit(':', 1)[1]
        else:
            self.docker_image_tag = ''
        self.docker_image_registry = os.path.dirname(repo_tag).split('/')[0]
        try:
            # This is the 'abc' in 'registry/abc/bla:latest'
            self.owner_namespace = os.path.dirname(repo_tag).split('/', 1)[1]
        except IndexError:
            self.owner_namespace = ''

    def is_docker_container(self):
        return True

    def _set_environment_specific_options(self,
                                          container_opts={}):
        """
        This function is used to setup these environment specific fields:
        namespace, log_prefix, and logfile_links.

        The environment is defined by the --environment argument.
        """

        logger.info('setup_namespace_and_metadata: long_id=' +
                    self.long_id)

        _map = container_opts.get('long_id_to_namespace_map', {})
        if self.long_id in _map:
            self.namespace = _map[self.long_id]
            self.log_prefix = ''
            self.logs_list_input = []
            return

        host_namespace = container_opts.get('host_namespace', 'undefined')
        options = defaults.DEFAULT_CRAWL_OPTIONS
        default_logs = options['logcrawler']['default_log_files']

        try:
            _options = {'root_fs': self.root_fs, 'type': 'docker',
                        'name': self.name, 'host_namespace': host_namespace,
                        'container_logs': default_logs}
            env = plugins_manager.get_runtime_env_plugin()
            namespace = env.get_container_namespace(
                self.long_id, _options)
            if not namespace:
                _env = env.get_environment_name()
                logger.warning('Container %s does not have %s '
                               'metadata.' % (self.short_id, _env))
                raise ContainerInvalidEnvironment('')
            self.namespace = namespace

            self.log_prefix = env.get_container_log_prefix(
                self.long_id, _options)

            self.logs_list_input.extend([LogFileLink(name=log['name'])
                                         for log in
                                         env.get_container_log_file_list(
                                         self.long_id, _options)])
        except ValueError:
            # XXX-kollerr: plugins are not supposed to throw ValueError
            logger.warning('Container %s does not have a valid alchemy '
                           'metadata json file.' % self.short_id)
            raise ContainerInvalidEnvironment()

    def _set_mounts_list(self):
        """
        Create self.mounts out of Volumes for old versions of Docker
        """

        if not self.mounts and self.volumes:
            self.mounts = [{'Destination': vol,
                            'Source': self.volumes[vol]}
                           for vol in self.volumes]
        elif not self.mounts and not self.volumes:
            self.mounts = []

    # Find the mount point of the specified cgroup

    def _get_cgroup_dir(self, dev=''):
        paths = [os.path.join('/cgroup/', dev),
                 os.path.join('/sys/fs/cgroup/', dev)]
        for path in paths:
            if os.path.ismount(path):
                return path

        # Try getting the mount point from /proc/mounts
        for l in open('/proc/mounts', 'r'):
            _type, mnt, _, _, _, _ = l.split(' ')
            if _type == 'cgroup' and mnt.endswith('cgroup/' + dev):
                return mnt

        raise ContainerWithoutCgroups('Can not find the cgroup dir')

    def get_memory_cgroup_path(self, node='memory.stat'):
        return os.path.join(self._get_cgroup_dir('memory'), 'docker',
                            self.long_id, node)

    def get_cpu_cgroup_path(self, node='cpuacct.usage'):
        # In kernels 4.x, the node is actually called 'cpu,cpuacct'
        cgroup_dir = (self._get_cgroup_dir('cpuacct') or
                      self._get_cgroup_dir('cpu,cpuacct'))
        return os.path.join(cgroup_dir, 'docker', self.long_id, node)

    def __str__(self):
        return str(self.__dict__)

    def link_logfiles(self,
                      options=defaults.DEFAULT_CRAWL_OPTIONS):

        host_log_dir = self._get_logfiles_links_dest(
            options['logcrawler']['host_log_basedir']
        )

        logger.debug('Linking log files for container %s' % self.short_id)

        # create an empty dir for the container logs

        if not os.path.exists(host_log_dir):
            os.makedirs(host_log_dir)

        # Create a symlink from src to dst

        for log in self.logs_list:
            dest = log.get_dest()
            try:
                if not os.path.exists(log.source):
                    logger.debug(
                        'Log file %s does not exist, but linking it anyway'
                        % log.source)
                dest_dir = os.path.dirname(dest)
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)
                os.symlink(log.source, dest)
                logger.info(
                    'Linking container %s %s logfile %s -> %s' %
                    (self.short_id, log.name, log.source, dest))
            except (OSError, IOError) as e:
                logger.debug(e)
                logger.debug('Link already exists: %s -> %s'
                             % (log.source, dest))
            except Exception as e:
                logger.warning(e)

        # Keep record of what is linked in a file.

        try:
            log_types_file = options['logcrawler']['log_types_file']
            types_host_log_path = os.path.join(host_log_dir,
                                               log_types_file)
            with open(types_host_log_path, 'w') as outfile:
                logs_dict = [{'name': log.name, 'type': log.type}
                             for log in self.logs_list]
                json.dump(logs_dict, outfile)
        except (OSError, IOError) as e:
            # Not a critical error: move on
            logger.exception(e)

    def unlink_logfiles(self,
                        options=defaults.DEFAULT_CRAWL_OPTIONS):

        host_log_dir = self._get_logfiles_links_dest(
            options['logcrawler']['host_log_basedir']
        )

        logger.info('Un-linking log files for container %s.'
                    % self.short_id)

        logger.info('Trying to delete this directory and its symlinks: %s.'
                    % host_log_dir)
        assert(host_log_dir.startswith('/var/log/crawler_container_logs/'))

        try:
            shutil.rmtree(host_log_dir)
        except (IOError, OSError) as exc:
            logger.error('Could not delete directory %s: %s' %
                         (host_log_dir, exc))

    def _parse_user_input_logs(self, var='LOG_LOCATIONS'):
        """
        The user can provide a list of logfiles in a container for us
        to maintain links to. This list of log files is passed as with
        the `var` environment variable.
        """

        container = self
        logs = []  # list of LogFileLink's
        try:
            logs = [LogFileLink(name=name) for name in
                    misc.get_process_env(container.pid)[var].split(',')]
        except (IOError, KeyError, ValueError) as e:
            logger.debug('There is a problem with the env. variables: %s' % e)
        return logs

    def _set_logs_list_input(self):
        """
        Sets the list of container logs that we should maintain links for.

        The paths are relative to the filesystem of the container. For example
        the path for /var/log/messages in the container will be just
        /var/log/messages in this list.
        """

        self.logs_list_input = self._parse_user_input_logs(var='LOG_LOCATIONS')

    def _expand_and_map_log_link(self, log, host_log_dir, rootfs_path):
        """
        Returns a list of LogFileLinks with all the fields set after
        expanding the globs and mapping mount points.
        """
        _logs = []
        if not self.mounts:
            source = misc.join_abs_paths(rootfs_path, log.name)
            if "*" in source:
                _logs = [LogFileLink(name=log.name,
                                     source=s,
                                     type=log.type,
                                     dest=s.split(rootfs_path, 1)[1],
                                     host_log_dir=host_log_dir)
                         for s in glob.glob(source)]
            else:
                _logs = [LogFileLink(name=log.name,
                                     type=log.type,
                                     source=source,
                                     dest=log.name,
                                     host_log_dir=host_log_dir)]

        for mount in self.mounts:
            mount_src = mount['Source']
            mount_dst = mount['Destination']
            if log.name.startswith(mount['Destination']):
                source = log.name.replace(mount_dst, mount_src)
                if "*" in source:
                    _logs = [LogFileLink(name=log.name,
                                         source=s,
                                         type=log.type,
                                         dest=s.replace(mount_src,
                                                        mount_dst),
                                         host_log_dir=host_log_dir)
                             for s in glob.glob(source)]
                else:
                    _logs = [LogFileLink(name=log.name,
                                         source=source,
                                         dest=log.name,
                                         type=log.type,
                                         host_log_dir=host_log_dir)]
            else:
                source = misc.join_abs_paths(rootfs_path, log.name)
                if "*" in source:
                    _logs = [LogFileLink(name=log.name,
                                         source=s,
                                         type=log.type,
                                         dest=s.split(rootfs_path, 1)[1],
                                         host_log_dir=host_log_dir)
                             for s in glob.glob(source)]
                else:
                    _logs = [LogFileLink(name=log.name,
                                         source=source,
                                         dest=log.name,
                                         type=log.type,
                                         host_log_dir=host_log_dir)]
        return _logs

    def _set_logs_list(self, options=defaults.DEFAULT_CRAWL_OPTIONS):
        """
        Initializes the LogFileLinks list in `self.logs_list`
        """

        host_log_dir = self._get_logfiles_links_dest(
            options['logcrawler']['host_log_basedir']
        )

        self.logs_list = []

        rootfs_path = self.root_fs
        if not rootfs_path:
            logger.warning(
                'Container %s does not have a rootfs_path set' %
                self.short_id)
            return

        # remove relative paths
        for log in self.logs_list_input:
            # remove relative paths
            if (not os.path.isabs(log.name)) or ('../' in log.name):
                logger.warning('User provided a log file path that is not '
                               'absolute: %s' % log.name)
                continue

            _logs = self._expand_and_map_log_link(log,
                                                  host_log_dir,
                                                  rootfs_path)
            for log in _logs:
                if log not in self.logs_list:
                    self.logs_list.append(log)

        logger.debug('logmap %s' % self.logs_list)

        # Link the container json log file name if there is one

        try:
            docker_log_source = get_docker_container_json_logs_path(
                self.long_id, self.inspect)
            docker_log_dest = os.path.join(host_log_dir,
                                           self.DOCKER_JSON_LOG_FILE)
            self.logs_list.append(LogFileLink(name=self.DOCKER_JSON_LOG_FILE,
                                              type=None,
                                              source=docker_log_source,
                                              dest=docker_log_dest))
        except DockerutilsNoJsonLog as e:
            logger.exception(e)

    def _get_logfiles_links_dest(
        self,
        host_log_basedir
    ):
        """
        Returns the path in the host file system where the container's log
        files should be linked to.
        """

        return os.path.join(host_log_basedir, self.log_prefix)
