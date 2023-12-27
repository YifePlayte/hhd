import argparse
import fcntl
import logging
import os
import signal
from os.path import join
from threading import Condition, RLock
from threading import Event as TEvent
from threading import Lock
from time import sleep
from typing import Sequence, cast

import pkg_resources

from .logging import set_log_plugin, setup_logger, update_log_plugins
from .plugins import (
    Config,
    Emitter,
    Event,
    HHDAutodetect,
    HHDPlugin,
    HHDSettings,
    load_relative_yaml,
)
from .plugins.settings import (
    get_default_state,
    load_profile_yaml,
    load_state_yaml,
    merge_settings,
    save_profile_yaml,
    save_state_yaml,
    validate_config,
)
from .utils import expanduser, fix_perms, get_context

logger = logging.getLogger(__name__)

CONFIG_DIR = os.environ.get("HHD_CONFIG_DIR", "~/.config/hhd")

ERROR_DELAY = 5
POLL_DELAY = 2
MODIFY_DELAY = 0.1


class EmitHolder(Emitter):
    def __init__(self, condition: Condition) -> None:
        self._events = []
        self._condition = condition

    def __call__(self, event: Event | Sequence[Event]) -> None:
        with self._condition:
            if isinstance(event, Sequence):
                self._events.extend(event)
            else:
                self._events.append(event)
            self._condition.notify_all()

    def get_events(self, timeout: int = -1) -> Sequence[Event]:
        with self._condition:
            if not self._events and timeout != -1:
                self._condition.wait()
            ev = self._events
            self._events = []
            return ev

    def has_events(self):
        with self._condition:
            return bool(self._events)


def notifier(ev: TEvent, cond: Condition):
    def _inner(sig, frame):
        with cond:
            ev.set()
            cond.notify_all()

    return _inner


def main():
    parser = argparse.ArgumentParser(
        prog="HHD: Handheld Daemon main interface.",
        description="Handheld Daemon is a daemon for managing the quirks inherent in handheld devices.",
    )
    parser.add_argument(
        "-u",
        "--user",
        default=None,
        help="The user whose home directory will be used to store the files (~/.config/hhd).",
        dest="user",
    )
    args = parser.parse_args()
    user = args.user

    # Setup temporary logger for permission retrieval
    ctx = get_context(user)
    if not ctx:
        print(f"Could not get user information. Exiting...")
        return

    detectors: dict[str, HHDAutodetect] = {}
    plugins: dict[str, Sequence[HHDPlugin]] = {}
    cfg_fds = []

    # HTTP data
    https = None
    prev_http_cfg = None

    try:
        set_log_plugin("main")
        setup_logger(join(CONFIG_DIR, "log"), ctx=ctx)

        for autodetect in pkg_resources.iter_entry_points("hhd.plugins"):
            detectors[autodetect.name] = autodetect.resolve()

        logger.info(f"Found plugin providers: {', '.join(list(detectors))}")

        logger.info(f"Running autodetection...")
        for name, autodetect in detectors.items():
            plugins[name] = autodetect([])

        plugin_str = "Loaded the following plugins:"
        for pkg_name, sub_plugins in plugins.items():
            plugin_str += (
                f"\n  - {pkg_name:>8s}: {', '.join(p.name for p in sub_plugins)}"
            )
        logger.info(plugin_str)

        # Get sorted plugins
        sorted_plugins: Sequence[HHDPlugin] = []
        for plugs in plugins.values():
            sorted_plugins.extend(plugs)
        sorted_plugins.sort(key=lambda x: x.priority)

        if not sorted_plugins:
            logger.error(f"No plugins started, exiting...")
            return

        # Open plugins
        lock = RLock()
        cond = Condition(lock)
        emit = EmitHolder(cond)
        for p in sorted_plugins:
            set_log_plugin(getattr(p, "log") if hasattr(p, "log") else "ukwn")
            p.open(emit, ctx)
            update_log_plugins()
        set_log_plugin("main")

        # Compile initial configuration
        state_fn = expanduser(join(CONFIG_DIR, "state.yml"), ctx)
        token_fn = expanduser(join(CONFIG_DIR, "token"), ctx)
        settings: HHDSettings = {}

        # Load profiles
        profiles = {}
        templates = {}
        conf = Config({})
        profile_dir = expanduser(join(CONFIG_DIR, "profiles"), ctx)
        os.makedirs(profile_dir, exist_ok=True)
        fix_perms(profile_dir, ctx)

        # Monitor config files for changes
        should_initialize = TEvent()
        initial_run = True
        should_exit = TEvent()
        signal.signal(signal.SIGPOLL, notifier(should_initialize, cond))
        signal.signal(signal.SIGINT, notifier(should_exit, cond))
        signal.signal(signal.SIGTERM, notifier(should_exit, cond))

        while not should_exit.is_set():
            #
            # Configuration
            #

            # Initialize if files changed
            if should_initialize.is_set() or initial_run:
                # wait a bit to allow other processes to save files
                if not initial_run:
                    sleep(POLL_DELAY)
                initial_run = False
                set_log_plugin("main")
                logger.info(f"Reloading configuration.")

                # Settings
                hhd_settings = {"hhd": load_relative_yaml("settings.yml")}
                settings = merge_settings(
                    [*[p.settings() for p in sorted_plugins], hhd_settings]
                )

                # State
                new_conf = load_state_yaml(state_fn, settings)
                if not new_conf:
                    if conf.conf:
                        logger.warning(f"Using previous configuration.")
                    else:
                        logger.info(f"Using default configuration.")
                        conf = get_default_state(settings)
                else:
                    conf = new_conf

                # Profiles
                profiles = {}
                templates = {}
                os.makedirs(profile_dir, exist_ok=True)
                fix_perms(profile_dir, ctx)
                for fn in os.listdir(profile_dir):
                    if not fn.endswith(".yml"):
                        continue
                    name = fn.replace(".yml", "")
                    s = load_profile_yaml(join(profile_dir, fn))
                    if s:
                        validate_config(s, settings, use_defaults=False)
                        if name.startswith("_"):
                            templates[name] = s
                        else:
                            # Profiles are shared so lock when accessing
                            # Configs have their own locks and are safe
                            with lock:
                                profiles[name] = s
                if profiles:
                    logger.info(
                        f"Loaded the following profiles (and state):\n[{', '.join(profiles)}]"
                    )
                else:
                    logger.info(f"No profiles found.")

                # Monitor files for changes
                for fd in cfg_fds:
                    try:
                        fcntl.fcntl(fd, fcntl.F_NOTIFY, 0)
                        os.close(fd)
                    except Exception:
                        pass
                cfg_fds = []
                cfg_fns = [
                    CONFIG_DIR,
                    join(CONFIG_DIR, "profiles"),
                ]
                for fn in cfg_fns:
                    fd = os.open(expanduser(fn, ctx), os.O_RDONLY)
                    fcntl.fcntl(
                        fd,
                        fcntl.F_NOTIFY,
                        fcntl.DN_CREATE
                        | fcntl.DN_DELETE
                        | fcntl.DN_MODIFY
                        | fcntl.DN_RENAME
                        | fcntl.DN_MULTISHOT,
                    )
                    cfg_fds.append(fd)

                # Initialize http server
                http_cfg = conf["hhd.http"]
                if http_cfg != prev_http_cfg:
                    prev_http_cfg = http_cfg
                    if https:
                        https.close()
                    if http_cfg["enable"].to(bool):
                        from .http import HHDHTTPServer

                        port = http_cfg["port"].to(int)
                        localhost = http_cfg["localhost"].to(bool)
                        use_token = http_cfg["token"].to(bool)

                        # Generate security token
                        if use_token:
                            import hashlib
                            import random

                            token = hashlib.sha256(
                                str(random.random()).encode()
                            ).hexdigest()
                            with open(token_fn, "w") as f:
                                os.chmod(token_fn, 0o600)
                                f.write(token)
                        else:
                            token = None

                        set_log_plugin("rest")
                        https = HHDHTTPServer(localhost, port, token)
                        https.update(settings, conf, profiles, emit)
                        https.open()
                        update_log_plugins()
                        set_log_plugin("main")

                should_initialize.clear()
                logger.info(f"Initialization Complete!")

            #
            # Plugin loop
            #

            # Process events
            settings_changed = False
            for ev in emit.get_events():
                match ev["type"]:
                    case "settings":
                        settings_changed = True
                    case "profile":
                        if ev["name"] in profiles:
                            profiles[ev["name"]].update(ev["config"].conf)
                        else:
                            with lock:
                                profiles[ev["name"]] = ev["config"]
                        validate_config(
                            profiles[ev["name"]], settings, use_defaults=False
                        )
                    case "apply":
                        if ev["name"] in profiles:
                            conf.update(profiles[ev["name"]].conf)
                    case "state":
                        conf.update(ev["config"].conf)
                    case other:
                        logger.error(f"Invalid event type submitted: '{other}'")

            # Validate config
            validate_config(conf, settings)

            # If settings changed, the configuration needs to reload
            # but it needs to be saved first
            if settings_changed:
                should_initialize.set()

            # Plugins are promised that once they emit a
            # settings change they are not called with the old settings
            if not settings_changed:
                #
                # Plugin event loop
                #

                for p in reversed(sorted_plugins):
                    set_log_plugin(getattr(p, "log") if hasattr(p, "log") else "ukwn")
                    p.prepare(conf)
                    update_log_plugins()

                for p in sorted_plugins:
                    set_log_plugin(getattr(p, "log") if hasattr(p, "log") else "ukwn")
                    p.update(conf)
                    update_log_plugins()
                set_log_plugin("ukwn")

            #
            # Save loop
            #

            has_new = should_initialize.is_set()
            saved = False
            # Save existing profiles if open
            if save_state_yaml(state_fn, settings, conf):
                fix_perms(state_fn, ctx)
                saved = True
            for name, prof in profiles.items():
                fn = join(profile_dir, name + ".yml")
                if save_profile_yaml(fn, settings, prof):
                    fix_perms(fn, ctx)
                    saved = True

            # Add template config
            if save_profile_yaml(
                join(profile_dir, "_template.yml"),
                settings,
                templates.get("_template", None),
            ):
                fix_perms(join(profile_dir, "_template.yml"), ctx)
                saved = True

            if not has_new and saved:
                # We triggered the interrupt, clear
                sleep(MODIFY_DELAY)
                should_initialize.clear()

            # Notify that events were applied
            if https:
                https.update(settings, conf, profiles, emit)

            # Wait for events
            with lock:
                if (
                    not should_exit.is_set()
                    and not settings_changed
                    and not should_initialize.is_set()
                    and not emit.has_events()
                ):
                    cond.wait(timeout=POLL_DELAY)

        set_log_plugin("main")
        logger.info(f"HHD Daemon received interrupt, stopping plugins and exiting.")
    finally:
        for fd in cfg_fds:
            try:
                os.close(fd)
            except Exception:
                pass
        if https:
            set_log_plugin("main")
            logger.info("Shutting down the REST API.")
            https.close()
        for plugs in plugins.values():
            for p in plugs:
                set_log_plugin("main")
                logger.info(f"Stopping plugin `{p.name}`.")
                set_log_plugin(getattr(p, "log") if hasattr(p, "log") else "ukwn")
                p.close()


if __name__ == "__main__":
    main()
