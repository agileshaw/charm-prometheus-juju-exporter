#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exporter snap helper.

Module focused on handling operations related to prometheus-juju-exporter snap.
"""
import logging
import os
from io import StringIO
import subprocess
from typing import Any, Dict, List, NamedTuple, Optional, Union

import yaml
from packaging import version

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class ExporterConfigError(Exception):
    """Indicates problem with configuration of exporter service."""


class ExporterConfig(NamedTuple):
    """Data class that holds information required for exporter configuration."""

    debug: Optional[str] = None
    customer: Optional[str] = None
    cloud: Optional[str] = None
    controller: Optional[str] = None
    ca_cert: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    interval: Optional[str] = None
    port: Optional[str] = None
    prefixes: Optional[str] = None
    match_interfaces: Optional[str] = None

    @property
    def controller_endpoint(self) -> Union[str, List[str]]:
        """Property that renders value for 'juju.controller_endpoint' option.

        Output is determined based on currently installed snap. Only
        prometheus-juju-exporter > 1.0.1 can accept list of strings in this config option.
        """
        if self.controller is None or self.controller == "":
            return ""

        endpoints: Union[str, List[str]] = self.controller.split(",")

        return endpoints

    def render(self) -> Dict[str, Union[Dict[str, Union[List[str], str, None]], str, None]]:
        """Return dict that can be written to an exporter config file as a yaml."""
        return {
            "debug": self.debug,
            "customer": {
                "name": self.customer,
                "cloud_name": self.cloud,
            },
            "juju": {
                "controller_endpoint": self.controller_endpoint,
                "controller_cacert": self.ca_cert,
                "username": self.user,
                "password": self.password,
            },
            "exporter": {
                "collect_interval": self.interval,
                "port": self.port,
            },
            "detection": {
                "virt_macs": self.prefixes.split(",") if self.prefixes else [],
                "match_interfaces": self.match_interfaces or ".*",
            },
        }


class ExporterOCI:
    """Class that handles operations of prometheus-juju-exporter snap and related services."""

    OCI_NAME = "prometheus-juju-exporter"
    OCI_CONFIG_DIR = f"/var/lib/{OCI_NAME}"
    OCI_CONFIG_PATH = f"{OCI_CONFIG_DIR}/config.yaml"
    _REQUIRED_CONFIG = [
        "customer.name",
        "customer.cloud_name",
        "juju.controller_endpoint",
        "juju.controller_cacert",
        "juju.username",
        "juju.password",
        "exporter.port",
        "exporter.collect_interval",
        "detection.virt_macs",
        "detection.match_interfaces",
    ]

    def _validate_required_options(self, config: Dict[str, Any]) -> List[str]:
        """Validate that config has all required options for snap to run."""
        missing_options = []
        for option in self._REQUIRED_CONFIG:
            config_value = config
            for identifier in option.split("."):
                config_value = config_value.get(identifier, {})
            if not config_value:
                missing_options.append(option)

        return missing_options

    @staticmethod
    def _validate_option_values(config: Dict[str, Any]) -> str:
        """Validate sane values for some of the config parameters where its feasible."""
        errors = ""

        # Verify that 'port' is number within valid port range.
        try:
            port = int(config["exporter"]["port"])
            if not 0 < port < 65535:
                errors += f"Port {port} is not valid port number.{os.linesep}"
        except ValueError:
            errors += f"Configuration option 'port' must be a number.{os.linesep}"
        except KeyError:
            pass  # Options was not in the config

        # Verify that 'collect_interval' is positive number.
        try:
            collect_interval = int(config["exporter"]["collect_interval"])
            if collect_interval < 1:
                errors += (
                    f"Configuration option 'collect_interval' must be a "
                    f"positive number.{os.linesep}"
                )
        except ValueError:
            errors += f"Configuration option 'collect_interval' must be a number.{os.linesep}"
        except KeyError:
            pass  # Options was not in the config

        return errors

    def validate_config(self, config: Dict[str, Any]) -> None:
        """Validate supplied config file for exporter service.

        :param config: config dictionary to be validated
        :raises:
            ExporterConfigError: In case the config does not pass the validation process. For
                example if the required fields are missing or values have unexpected format.
        """
        errors = ""

        missing_options = self._validate_required_options(config)
        if missing_options:
            missing_str = ", ".join(missing_options)
            errors += f"Following config options are missing: {missing_str}{os.linesep}"

        errors += self._validate_option_values(config)

        if errors:
            raise ExporterConfigError(errors)

    def apply_config(self, exporter_config: Dict[str, Any]) -> None:
        """Update configuration file for exporter service."""
        logger.info("Updating exporter service configuration.")
        self.validate_config(exporter_config)

        # if not os.path.exists(self.OCI_CONFIG_DIR):
        #     os.mkdir(self.OCI_CONFIG_DIR)
        # else:
        #     if not os.path.isdir(self.OCI_CONFIG_DIR):
        #         os.remove(self.OCI_CONFIG_DIR)
        #         os.mkdir(self.OCI_CONFIG_DIR)

        # with open(self.OCI_CONFIG_PATH, "w", encoding="utf-8") as config_file:
        data_file = StringIO()
        yaml.safe_dump(exporter_config, data_file)
        data_file.seek(0)
        ret = data_file.read()
        data_file.close()
        logger.info("Exporter configuration updated.")
        return ret

       
