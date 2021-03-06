#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Module containing the BaseStrategy."""

from __future__ import absolute_import, division, unicode_literals

import copy
import datetime
import itertools
import os
import time

import concurrent.futures
import farnsworth.config
from farnsworth.models.job import Job
import pykube.exceptions
import pykube.http
import pykube.objects
import requests.exceptions

from ..brains.toad import ToadBrain
import meister.log
import meister.kubernetes as kubernetes

LOG = meister.log.LOG.getChild('schedulers')

NUM_THREADS = int(os.environ.get('MEISTER_NUM_THREADS', '20'))


def cpu2float(cpu):
    """Internal helper function to convert Kubernetes CPU numbers to float."""
    if cpu.endswith("m"):
        cpu = int(cpu[:-1]) / 1000.
    return float(cpu)


def memory2int(memory):
    """Internal helper function to convert Kubernetes memory amount to integers."""
    multiplier = 1
    if memory.endswith("Ki"):
        multiplier = 1024
    elif memory.endswith("Mi"):
        multiplier = 1024 ** 2
    elif memory.endswith("Gi"):
        multiplier = 1024 ** 3
    return int(memory[:-2]) * multiplier

def _list_getter(c):
    return list(c.jobs)

class KubernetesScheduler(object):
    """Kubernetes scheduler class, should be inherited by actual schedulers."""

    def __init__(self):
        """Create base scheduler.

        Create the base scheduler, you should call this from your scheduler to
        make sure that all class variables are set up properly.
        """
        self._api = None
        self._node_capacities = None
        self._available_resources = None
        self._resources_cache_timeout = datetime.timedelta(seconds=1)
        self._resources_timestamp = datetime.datetime(1970, 1, 1, 0, 0, 0)

    def schedule(self, job):
        """Schedule the job with the specific resources."""
        LOG.debug("Scheduling job for job id %s", job.id)
        self.terminate(self._worker_name(job.id))
        self._schedule_kube_pod(job)

    @property
    def api(self):
        """Return the API we are working on."""
        if self._api is None:
            self._api = pykube.http.HTTPClient(kubernetes.from_env())
        return self._api

    @classmethod
    def _worker_name(cls, job_id):
        """Return the worker name for a specific job_id."""
        return "worker-{}".format(job_id)

    @classmethod
    def _is_kubernetes_unavailable(cls):
        """Check if running without Kubernetes"""
        return ('KUBERNETES_SERVICE_HOST' not in os.environ or
                os.environ['KUBERNETES_SERVICE_HOST'] == "")

    def _kube_pod_template(self, job):
        name = self._worker_name(job.id)
        # Job Resource Requirements
        if job.request_cpu is None:
            request_cpu = Job.request_cpu.default
        else:
            request_cpu = job.request_cpu
        if job.request_memory is None:
            request_memory = Job.request_memory.default
        else:
            request_memory = job.request_memory

        # Job Resource Limits
        if job.limit_cpu is None:
            limit_cpu = Job.limit_cpu.default
        elif request_cpu < job.limit_cpu:
            # CPU limit is set to a reasonable value
            limit_cpu = job.limit_cpu
        else:
            # Better add some padding
            limit_cpu = request_cpu * 2

        if job.limit_memory is None:
            limit_memory = Job.limit_memory.default
        elif request_memory < job.limit_memory:
            limit_memory = job.limit_memory
        else:
            limit_memory = request_memory * 2

        restart_policy = 'OnFailure' if job.restart else 'Never'
        volumes = [{'name': 'devshm', 'emptyDir': {'medium': 'Memory'}}]
        volume_mounts = [{'name': 'devshm', 'mountPath': '/dev/shm'}]
        security_context = {}

        if job.kvm_access:
            volumes.append({'name': 'devkvm', 'hostPath': {'path': '/dev/kvm'}})
            volume_mounts.append({'name': 'devkvm', 'mountPath': '/dev/kvm'})
            security_context['privileged'] = True

        if job.data_access:
            volumes.append({'name': 'data', 'hostPath': {'path': '/data'}})
            volume_mounts.append({'name': 'data', 'mountPath': '/data'})

        if os.environ.get('POSTGRES_USE_SLAVES') is not None:
            postgres_use_slaves = {'name': "POSTGRES_USE_SLAVES", 'value': "true"}
        else:
            postgres_use_slaves = None

        config = {
            'metadata': {
                'labels': {
                    'app': 'worker',
                    'worker': job.worker,
                    'job_id': str(job.id),
                },
                'name': name
            },
            'spec': {
                'restartPolicy': restart_policy,
                'containers': [
                    {
                        'name': name,
                        'image': os.environ['WORKER_IMAGE'],
                        'imagePullPolicy': os.environ['WORKER_IMAGE_PULL_POLICY'],
                        'resources': {
                            'requests': {
                                'cpu': str(request_cpu),
                                'memory': "{}Mi".format(request_memory)
                            },
                            'limits': {
                                'cpu': str(limit_cpu),
                                'memory': "{}Mi".format(limit_memory)
                            }
                        },
                        'env': filter(None, [
                            {'name': "JOB_ID", 'value': str(job.id)},
                            postgres_use_slaves,
                            {'name': "POSTGRES_DATABASE_USER",
                             'value': os.environ['POSTGRES_DATABASE_USER']},
                            {'name': "POSTGRES_DATABASE_PASSWORD",
                             'value': os.environ['POSTGRES_DATABASE_PASSWORD']},
                            {'name': "POSTGRES_DATABASE_NAME",
                             'value': os.environ['POSTGRES_DATABASE_NAME']},
                            {'name': "POSTGRES_MASTER_CONNECTIONS",
                             'value': "1"},
                            {'name': "POSTGRES_SLAVE_CONNECTIONS",
                             'value': "1"},
                        ]),
                        'volumeMounts': volume_mounts,
                        'securityContext': security_context
                    }
                ],
                'volumes': volumes
            }
        }
        return config

    # Currently, we are using aggregate resources instead of per node resources for ease of
    # scheduling. This is suboptimal because we might run into situation where we think we can
    # schedule a job but actually cannot. The example below is a generalization of this in case node
    # specific information is retained.
    #
    # There is a possibly problem that we might have to use almost the latest information because
    # the following might happen:
    #  - node a has availability 2 cores / 1GB
    #  - node b has availability 2 cores / 2GB
    #  - we schedule 2 cores / 1GB, update our dict to substract from a
    #  - Kubernetes schedules it to run on node b
    #  - we want to schedule 2 cores / 2GB, our dict thinks we can on b
    #  - Kubernetes fails to spawn the worker
    @property
    def _kube_resources(self):
        """Internal helper method to collect resource data for Kubernetes cluster."""
        assert isinstance(self.api, pykube.http.HTTPClient)

        # Return cached data
        if (datetime.datetime.now() - self._resources_timestamp) <= self._resources_cache_timeout:
            LOG.debug("Returning cached data...")
            return self._available_resources

        # Reset available resources
        self._available_resources = copy.copy(self._kube_total_capacity)

        # Collect fresh information from the Kubernetes API about all running pods
        def cleanup(pod):
            try:
                if pod.pending or pod.running:
                    try:
                        LOG.debug("Pod %s is taking up resources", pod.name)
                    except requests.exceptions.HTTPError, e:
                        LOG.error("Somehow failed at HTTP %s", e)
                    return pod
                elif pod.failed:
                    LOG.warning("Pod %s failed", pod.name)
                    pod.delete()
                elif pod.unknown:
                    LOG.warning("Pod %s in unknown state", pod.name)
                elif pod.succeeded:
                    LOG.debug("Pod %s succeeded", pod.name)
                    pod.delete()
                else:
                    LOG.debug("Pod %s is in a weird state", pod.name)
            except KeyError, e:
                LOG.error("Hit a KeyError %s", e)

        pods = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            pods = executor.map(cleanup, pykube.objects.Pod.objects(self.api))

        for pod in pods:
            if pod is None:
                continue

            if pod.obj['spec']['containers'][0]['resources']:
                try:
                    resources = pod.obj['spec']['containers'][0]['resources']['requests']
                except KeyError:
                    resources = pod.obj['spec']['containers'][0]['resources']['limits']
            else:
                resources = {'cpu': '0', 'memory': '0Ki'}

            self._available_resources['cpu'] -= cpu2float(resources['cpu'])
            self._available_resources['memory'] -= memory2int(resources['memory'])
            # We are assuming that each pod only has one container here.
            self._available_resources['pods'] -= 1

        self._resources_timestamp = datetime.datetime.now()

        self._available_resources['cpu'] *= float(os.environ['MEISTER_OVERPROVISIONING'])
        self._available_resources['memory'] *= float(os.environ['MEISTER_OVERPROVISIONING'])
        self._available_resources['pods'] *= float(os.environ['MEISTER_OVERPROVISIONING'])

        LOG.debug("Resources available: %s cores, %s GiB, %s pods",
                  self._available_resources['cpu'],
                  self._available_resources['memory'] // (1024**3),
                  self._available_resources['pods'])

        return self._available_resources

    @property
    def _kube_total_capacity(self):
        """Internal helper method to return the total capacity on the Kubernetes cluster."""
        resources = {'cpu': 0.0, 'memory': 0L, 'pods': 0}
        for capacity in self._kube_node_capacities.values():
            resources['cpu'] += capacity['cpu']
            resources['memory'] += capacity['memory']
            resources['pods'] += capacity['pods']

        LOG.debug("Total cluster capacity: %s cores, %s GiB, %s pods",
                  resources['cpu'], resources['memory'] // (1024**3),
                  resources['pods'])
        return resources

    @property
    def _kube_node_capacities(self):
        """Internal helper method to collect the total capacity on the Kubernetes cluster."""
        assert isinstance(self.api, pykube.http.HTTPClient)

        # Update node capacities, only ran once for the first update of availabe resources
        if self._node_capacities is None:
            nodes = pykube.objects.Node.objects(self.api).all()
            self._node_capacities = {}
            for node in nodes:
                cpu = cpu2float(node.obj['status']['capacity']['cpu'])
                memory = memory2int(node.obj['status']['capacity']['memory'])
                pods = int(node.obj['status']['capacity']['pods'])
                self._node_capacities[node.name] = {'cpu': cpu,
                                                    'memory': memory,
                                                    'pods': pods}
        return self._node_capacities

    def _schedule_kube_pod(self, job):
        """Internal method to schedule a job on Kubernetes."""
        assert isinstance(self.api, pykube.http.HTTPClient)
        config = self._kube_pod_template(job)

        try:
            pykube.objects.Pod(self.api, config).create()
        except requests.exceptions.HTTPError as error:
            if error.response.status_code == 409:
                LOG.warning("Job already scheduled %s", job.id)
            else:
                LOG.error("requests.HTTPError %s: %s",
                          error.response.status_code,
                          error.response.content)
        except pykube.exceptions.HTTPError as error:
            LOG.error("pykube.HTTPError %s", error)
            # TODO: We need to properly inspect the error
            # raise error

    def terminate(self, name):
        """Terminate worker 'name'."""
        assert isinstance(self.api, pykube.http.HTTPClient)
        # TODO: job might have shutdown gracefully in-between being identified
        # and being asked to get terminated; do we need to check if it exists?
        config = {'metadata': {'name': name},
                  'kind': 'Pod'}
        if pykube.objects.Pod(self.api, config).exists():
            LOG.debug("Terminating pod %s", config['metadata']['name'])
            pykube.objects.Pod(self.api, config).delete()


class BaseScheduler(KubernetesScheduler):
    """Base strategy.

    All other scheduling strategies should inherit from this Strategy.
    """

    def __init__(self, brain=None, creators=None, sleepytime=3):
        """Construct a base strategy object.

        The Base Strategy assumes that the Farnsworth API is setup already,
        and uses it directly.

        :keyword sleepytime: the amount to sleep between strategy runs.
        :keyword creators: list of creators yielding jobs.
        """
        self.brain = brain if brain is not None else ToadBrain()
        self.creators = creators if creators is not None else []
        self.sleepytime = sleepytime
        super(BaseScheduler, self).__init__()

        LOG.debug("Scheduler sleepytime: %d", self.sleepytime)
        LOG.debug("Job creators: %s", ", ".join(c.__class__.__name__
                                                for c in self.creators))

    def sleep(self):
        """Sleep a pre-defined interval."""
        LOG.debug("Sleepytime...")
        time.sleep(self.sleepytime)

    @property
    def jobs(self):
        """Return all jobs that all creators want to run."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            # Return 25 jobs at a time to speed up generator
            jobs_unordered_iter = executor.map(_list_getter,
                                               self.creators, chunksize=25)
            for job in itertools.chain.from_iterable(jobs_unordered_iter):
                yield job

    def _run(self):
        raise NotImplementedError("Implement it!")

    def run(self):
        """Run the scheduler."""
        if self._is_kubernetes_unavailable():
            # Run without actually scheduling
            with farnsworth.config.master_db.atomic():
                for job, priority in self.brain.sort(self.jobs):
                    kwargs = {df.name: getattr(job, df.name) for df in job.dirty_fields}
                    job, _ = type(job).get_or_create(**kwargs)
                    job.priority = priority
                    job.save()
        else:
            # Run internal scheduler method
            self._run()
