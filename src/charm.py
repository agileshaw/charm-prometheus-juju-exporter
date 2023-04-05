#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import hashlib
import logging
import os
import pathlib
from base64 import b64decode
from binascii import Error as Base64Error
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Union

import yaml
from charmhelpers.core import hookenv
from charmhelpers.fetch import snap
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops.charm import (
    CharmBase,
    ConfigChangedEvent,
    InstallEvent,
    StopEvent,
    UpdateStatusEvent,
    UpgradeCharmEvent,
)
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus, MaintenanceStatus, ModelError
from ops.pebble import Layer
from prometheus_interface.operator import (
    PrometheusConfigError,
    PrometheusConnected,
    PrometheusScrapeTarget,
)

from exporter import ExporterConfig, ExporterConfigError, ExporterOCI

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class PrometheusJujuExporterCharm(CharmBase):
    """Charm the service."""

    # Mapping between charm and snap configuration options
    OCI_CONFIG_MAP = {
        "debug": "debug",
        "customer": "customer.name",
        "cloud-name": "customer.cloud_name",
        "controller-url": "juju.controller_endpoint",
        "juju-user": "juju.username",
        "juju-password": "juju.password",
        "scrape-interval": "exporter.collect_interval",
        "scrape-port": "exporter.port",
        "virtual-macs": "detection.virt_macs",
        "match-interfaces": "detection.match_interfaces",
    }
    OCI_NAME = "prometheus-juju-exporter"
    OCI_CONFIG_DIR = f"/var/lib/{OCI_NAME}"
    OCI_CONFIG_PATH = f"{OCI_CONFIG_DIR}/config.yaml"

    def __init__(self, *args: Any) -> None:
        """Initialize charm."""
        super().__init__(*args)
        self.name = "pje"
        self.container = self.unit.get_container(self.name)
        self.exporter = ExporterOCI()
        self.prometheus_target = PrometheusScrapeTarget(self, "prometheus-scrape")
        self.current_config_hash = None

        self.framework.observe(self.on.pje_pebble_ready, self._on_pje_pebble_ready)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(
            self.prometheus_target.on.prometheus_available, self._on_prometheus_available
        )

        port = self.config["scrape-port"]
        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            relation_name="prometheus-k8s-scrape",
            jobs=[
                {
                    "static_configs": [{"targets": [f"*:{port}"]}],
                },
            ],
        )
        self.grafana_dashboard_provider = GrafanaDashboardProvider(
            self, relation_name="grafana-k8s-dashboard"
        )
        self.grafana_dashboard_provider._reinitialize_dashboard_data(inject_dropdowns=False)

    def get_controller_ca_cert(self) -> str:
        """Get CA certificate used by targeted Juju controller.

        CA certificate can be directly configured by `controller-ca-cert` option, if it is, the
        value is directly returned by this method. If it is not defined, a CA cert used by the
        controller that deploys this unit will be returned.
        """
        explicit_cert = self.config.get("controller-ca-cert", "")
        if explicit_cert:
            try:
                return b64decode(explicit_cert, validate=True).decode(encoding="ascii")
            except Base64Error as exc:
                logger.error(
                    "Config option 'controller-ca-cert' does not contain valid base64-encoded"
                    " data. Bad data: %s",
                    explicit_cert,
                )
                raise RuntimeError("Invalid base64 value in 'controller-ca-cert' option.") from exc

        agent_conf_path = pathlib.Path(hookenv.charm_dir()).joinpath("../agent.conf")
        with open(agent_conf_path, "r", encoding="utf-8") as conf_file:
            agent_conf = yaml.safe_load(conf_file)

        ca_cert = agent_conf.get("cacert")
        if not ca_cert:
            raise RuntimeError("Charm failed to fetch controller's CA certificate.")

        return ca_cert

    def generate_exporter_config(
        self,
    ) -> Dict[str, Union[Dict[str, Union[List[str], str, None]], str, None]]:
        """Generate exporter service config based on the values from charm config."""
        config = ExporterConfig(
            debug=self.config.get("debug"),
            customer=self.config.get("customer"),
            cloud=self.config.get("cloud-name"),
            controller=self.config.get("controller-url"),
            ca_cert=self.get_controller_ca_cert(),
            user=self.config.get("juju-user"),
            password=self.config.get("juju-password"),
            interval=self.config.get("scrape-interval"),
            port=self.config.get("scrape-port"),
            prefixes=self.config.get("virtual-macs"),
            match_interfaces=self.config.get("match-interfaces"),
        )

        return config.render()

    def reconfigure_scrape_target(self) -> None:
        """Update scrape target configuration in related Prometheus application.

        Note: this function has no effect if there's no application related via
        'prometheus-scrape'.
        """
        port = self.config["scrape-port"]
        interval_minutes = self.config["scrape-interval"]
        interval = interval_minutes * 60
        timeout = self.config["scrape-timeout"]
        try:
            self.prometheus_target.expose_scrape_target(
                port, "/metrics", scrape_interval=f"{interval}s", scrape_timeout=f"{timeout}s"
            )
        except PrometheusConfigError as exc:
            logger.error("Failed to configure prometheus scrape target: %s", exc)
            raise exc

    def reconfigure_open_ports(self) -> None:
        """Update ports that juju shows as 'opened' in units' status."""
        new_port = self.config["scrape-port"]

        for port_spec in hookenv.opened_ports():
            old_port, protocol = port_spec.split("/")
            logger.debug("Setting port %s as closed.", old_port)
            hookenv.close_port(old_port, protocol)

        logger.debug("Setting port %s as opened.", new_port)
        hookenv.open_port(new_port)
    
    def _on_pje_pebble_ready(self, event):
        self._configure()

    def _on_upgrade_charm(self, _: UpgradeCharmEvent) -> None:
        """Process charm upgrade event.

        Since this event is triggered also when new resource is attached to the charm,
        we must re-install the snap and re-apply configuration
        """
        self._on_config_changed(None)

    def _on_config_changed(self, _: Optional[ConfigChangedEvent]) -> None:
        """Handle changed configuration."""
        self._configure()
        
    
    def _configure(self):
        if not self.container.can_connect():
            return
        logger.info("Processing new charm configuration.")
        self.unit.status = MaintenanceStatus("Processing new charm configuration.")
        restart = False

        exporter_config = self.generate_exporter_config()
        exporter_config_hash = hashlib.sha256(str(exporter_config).encode("utf-8")).hexdigest()
        logger.info("exporter_config_hash: %s", exporter_config_hash)
        logger.info("current_config_hash: %s", self.current_config_hash)

        if exporter_config_hash != self.current_config_hash:
            logger.info("inside hash compare")
            try:
                config_data = self.exporter.apply_config(exporter_config)
                logger.info("config_data: %s", config_data)
                self.container.push(self.OCI_CONFIG_PATH, config_data, make_dirs=True)
                logger.info("push: %s", self.OCI_CONFIG_PATH)
                self.current_config_hash = exporter_config_hash
            except ExporterConfigError as exc:
                # Replace snap config names with their charm equivalents
                err_msg = str(exc)
                for charm_option, oci_option in self.OCI_CONFIG_MAP.items():
                    err_msg = err_msg.replace(oci_option, charm_option)

                logger.error(err_msg)
                self.unit.status = BlockedStatus("Invalid configuration. Please see logs.")
                return
            except ConnectionError:
                logger.error(
                    "Could not push datasource config. Pebble refused connection. Shutting down?"
                )
            restart = True
        
        if self.container.get_plan().services != self._build_layer().services:
            restart = True

        self.reconfigure_scrape_target()
        self.reconfigure_open_ports()

        logger.info("restart: %s", restart)
        if restart:
            self.restart_pje()
        else:
            # All clear, move to active.
            # We can basically only get here if the charm is completely restarted, but all of
            # the configs are correct, with the correct pebble plan, such as a node reboot.
            #
            # A node reboot does not send any identifiable events (`start`, `pebble_ready`), so
            # this is more or less the 'fallthrough' part of a case statement
            if not isinstance(self.unit.status, ActiveStatus):
                self.unit.status = ActiveStatus()
    
    def restart_pje(self):
        layer = self._build_layer()
        try:
            self.container.add_layer(self.name, layer, combine=True)
            logging.info("Added updated layer '%s' to Pebble plan", self.name)
            self.container.restart(self.name)
            logging.info("Restarted %s service", self.name)
            self.unit.status = ActiveStatus()
        except Exception as e:
            # debug because, on initial container startup when Grafana has an open lock and is
            # populating, this comes up with ERRCODE: 26
            logger.error("Could not restart prometheus-juju-exporter and build new layer: {}".format(e))

    def _build_layer(self) -> Layer:
        return Layer(
            {
                "summary": "prometheus-juju-exporter layer",
                "description": "prometheus-juju-exporter layer",
                "services": {
                    self.name: {
                        "override": "replace",
                        "summary": "prometheus-juju-exporter service",
                        "command": "/bin/python3 -m prometheus_juju_exporter.cli",
                        "startup": "enabled",
                    }
                },
            }
        )
    

    def _on_prometheus_available(self, _: PrometheusConnected) -> None:
        """Trigger configuration of a prometheus scrape target."""
        self.reconfigure_scrape_target()


if __name__ == "__main__":  # pragma: nocover
    main(PrometheusJujuExporterCharm)
