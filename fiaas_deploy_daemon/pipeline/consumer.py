#!/usr/bin/env python
# -*- coding: utf-8
from __future__ import absolute_import

import time
import os
import re
import json
import logging

from kafka import KafkaConsumer
from prometheus_client import Counter
from requests import HTTPError

from ..base_thread import DaemonThread


ENV_PATTERN = re.compile("^(.+?)\d*$")
ALLOW_WITHOUT_MESSAGES_S = int(os.getenv('ALLOW_WITHOUT_MESSAGES_MIN', 30)) * 60


class Consumer(DaemonThread):
    """Listen for pipeline events and generate AppSpecs for the deployer

    Requires the following environment variables:
    - KAFKA_PIPELINE_SERVICE_HOST: Comma separated list of kafka brokers
    - KAFKA_PIPELINE_SERVICE_PORT: Port kafka listens on
    """

    def __init__(self, deploy_queue, config, reporter, spec_factory):
        super(Consumer, self).__init__()
        self._logger = logging.getLogger(__name__)
        self._deploy_queue = deploy_queue
        self._consumer = None
        self._config = config
        self._environment = self._extract_env(config)
        self._reporter = reporter
        self._spec_factory = spec_factory
        self._last_message_timestamp = int(time.time())

    def __call__(self):
        if self._consumer is None:
            self._connect_kafka()
        message_counter = Counter("pipeline_message_received", "A message from pipeline received")
        deploy_counter = Counter("pipeline_deploy_triggered", "A message caused a deploy to be triggered")
        for message in self._consumer:
            message_counter.inc()
            self._last_message_timestamp = int(time.time())
            event = self._deserialize(message)
            self._logger.debug("Got event: %r", event)
            if event[u"environment"] == self._environment:
                try:
                    app_spec = self._create_spec(event)
                    self._deploy_queue.put(app_spec)
                    self._reporter.register(app_spec.image, event[u"callback_url"])
                    deploy_counter.inc()
                except (NoDockerArtifactException, NoFiaasArtifactException):
                    self._logger.debug("Ignoring event %r with missing artifacts", event)
                except HTTPError:
                    self._logger.exception("Failure when downloading FIAAS-config")

    def _connect_kafka(self):
        self._consumer = KafkaConsumer(
                "internal.pipeline.deployment",
                bootstrap_servers=self._build_connect_string("kafka_pipeline")
        )

    def _create_spec(self, event):
        artifacts = event[u"artifacts_by_type"]
        if u"docker" not in artifacts:
            raise NoDockerArtifactException()
        if u"fiaas" not in artifacts:
            raise NoFiaasArtifactException()
        name = event[u"project_name"]
        artifacts = event[u"artifacts_by_type"]
        image = artifacts[u"docker"]
        fiaas_url = artifacts[u"fiaas"]
        return self._spec_factory(name, image, fiaas_url)

    def _deserialize(self, message):
        return json.loads(message.value)

    def _build_connect_string(self, service):
        host, port = self._config.resolve_service(service)
        connect = ",".join("{}:{}".format(host, port) for host in host.split(","))
        return connect

    def _is_recieving_messages(self):
        # TODO: this is a hack to handle the fact that we sometimes seems to loose contact with kafka
        self._logger.debug("No message for %r seconds", int(time.time()) - self._last_message_timestamp)
        return self._last_message_timestamp > int(time.time()) - ALLOW_WITHOUT_MESSAGES_S

    @staticmethod
    def _extract_env(config):
        m = ENV_PATTERN.match(config.target_cluster)
        if m:
            return m.group(1)
        return config.target_cluster


class NoDockerArtifactException(Exception):
    pass


class NoFiaasArtifactException(Exception):
    pass