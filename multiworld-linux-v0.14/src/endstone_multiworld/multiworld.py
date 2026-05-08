"""MultiWorld plugin for Endstone.

This single plugin adapts based on whether it's running on the hub or in a world server,
so you only need one pip install. The plugin detects which mode it should be in
by checking the current working directory at import time.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread

from endstone.plugin import Plugin
from endstone.command import Command, CommandSender
from endstone import ColorFormat, Player
from endstone.form import ActionForm, ModalForm, MessageForm, Dropdown, TextInput, Label, Toggle


# ---- Detect at import time whether we're inside a world or on the hub ----
WORLDS_BASE_DIR_CHECK = "/endstone/worlds"
_cwd = os.path.abspath(os.getcwd())
_IS_HUB = WORLDS_BASE_DIR_CHECK not in _cwd
_IS_WORLD = not _IS_HUB

# The set of commands depends on which mode we're in
if _IS_HUB:
    _COMMANDS = {
        "world": {
            "description": "Manage your personal world",
            "usages": [
                "/world",
                "/world (delete|leave|list|browse|like|dislike|profile|friends|achievements|leaderboard|template|clone)<action: WorldAction>",
                "/world (create)<action: WorldCreate> (normal|flat)<world_type: WorldType> (survival|creative|adventure)<gamemode: WorldGamemode>",
                "/world (invite|uninvite|join|locate)<action: WorldActionTarget> <target: player>",
            ],
            "permissions": ["multiworld.command.world"],
        }
    }
else:
    _COMMANDS = {
        "world": {
            "description": "Manage your world membership",
            "usages": [
                "/world",
                "/world (leave|list|browse|like|dislike|profile|friends|achievements|leaderboard)<action: WorldAction>",
                "/world (invite|uninvite|locate)<action: WorldActionTarget> <target: player>",
            ],
            "permissions": ["multiworld.command.world"],
        }
    }

_PERMISSIONS = {
    "multiworld.command.world": {
        "description": "Allows use of the /world command",
        "default": True,
    }
}


class MultiWorld(Plugin):
    name = "multiworld"
    version = "0.14.0"
    api_version = "0.6"
    commands = _COMMANDS
    permissions = _PERMISSIONS

    HUB_PORT = 37373
    WORLD_PORT_START = 37374
    WORLD_PORT_END = 37399
    WORLDS_BASE_DIR = "/endstone/worlds"
    DATA_FILE = "/endstone/multiworld_data.json"
    MESSAGES_FILE = "/endstone/multiworld_messages.json"
    CONFIG_FILE = "/endstone/multiworld_config.json"
    PRESENCE_FILE = "/endstone/multiworld_presence.json"
    PLAYERS_FILE = "/endstone/multiworld_players.json"
    EMPTY_TIMEOUT = 300
    MESSAGE_POLL_INTERVAL = 2
    PRESENCE_UPDATE_INTERVAL = 5
    DEFAULT_TRANSFER_HOST = "127.0.0.1"

    def on_enable(self):
        if _IS_HUB:
            self._enable_hub()
        else:
            self._enable_world()

    def _enable_hub(self):
        self.logger.info(f"{ColorFormat.GREEN}MultiWorld (HUB) enabled!")
        self._mw_config = self._load_or_create_config()
        self.transfer_host = self._mw_config.get("transfer_host", self.DEFAULT_TRANSFER_HOST)
        # Auto-detect the hub's port from its server.properties so /world leave works
        self.hub_port = self._read_hub_port_from_properties() or self.HUB_PORT
        self.logger.info(f"Using transfer_host: {self.transfer_host}, hub_port: {self.hub_port}")
        self.data = self.load_data()
        self.running_worlds = {}
        self._pending_deletes = set()  # paths that have pending background cleanup
        os.makedirs(self.WORLDS_BASE_DIR, exist_ok=True)
        if not os.path.exists(self.MESSAGES_FILE):
            self._write_messages({"messages": []})
        # Save the detected hub port to config so worlds can read it
        if self._mw_config.get("hub_port") != self.hub_port:
            self._mw_config["hub_port"] = self.hub_port
            try:
                with open(self.CONFIG_FILE, "w") as f:
                    json.dump(self._mw_config, f, indent=2)
            except Exception as e:
                self.logger.warning(f"Failed to save hub_port to config: {e}")
        self._kill_processes_using_path(self.WORLDS_BASE_DIR)
        self.cleanup_thread = Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()
        self.message_thread = Thread(target=self._message_poll_loop, daemon=True)
        self.message_thread.start()
        self.presence_thread = Thread(target=self._presence_loop, daemon=True)
        self.presence_thread.start()

    def _enable_world(self):
        self.logger.info(f"{ColorFormat.GREEN}MultiWorld (WORLD) enabled!")
        self.transfer_host = self._load_transfer_host_only()
        self.hub_port = self._load_hub_port_from_config() or self.HUB_PORT
        self.logger.info(f"Using transfer_host: {self.transfer_host}, hub_port: {self.hub_port}")
        if not os.path.exists(self.MESSAGES_FILE):
            self._write_messages({"messages": []})
        self.message_thread = Thread(target=self._message_poll_loop, daemon=True)
        self.message_thread.start()
        self.presence_thread = Thread(target=self._presence_loop, daemon=True)
        self.presence_thread.start()

    def _read_hub_port_from_properties(self):
        """Read server-port from the current cwd's server.properties."""
        props_path = os.path.join(os.getcwd(), "server.properties")
        if not os.path.isfile(props_path):
            return None
        try:
            with open(props_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("server-port="):
                        return int(line.split("=", 1)[1].strip())
        except Exception as e:
            self.logger.warning(f"Failed to read server.properties: {e}")
        return None

    def _load_hub_port_from_config(self):
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                    return cfg.get("hub_port")
        except Exception:
            pass
        return None

    def on_disable(self):
        if _IS_HUB:
            for owner, info in list(getattr(self, "running_worlds", {}).items()):
                self._stop_world(owner)
            if hasattr(self, "data"):
                self.save_data()
        self.logger.info("MultiWorld disabled.")

    # ---- Config ----
    def _load_or_create_config(self):
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(f"Failed to read config: {e}")
        host = self._auto_detect_host()
        config = {
            "transfer_host": host,
            "_comment": "transfer_host is the address players use to connect. Edit this if auto-detection picked the wrong one.",
        }
        try:
            with open(self.CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            self.logger.info(f"Created multiworld config at {self.CONFIG_FILE} with transfer_host={host}")
        except Exception as e:
            self.logger.warning(f"Failed to write config: {e}")
        return config

    def _load_transfer_host_only(self):
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                    return cfg.get("transfer_host", self.DEFAULT_TRANSFER_HOST)
        except Exception:
            pass
        return self.DEFAULT_TRANSFER_HOST

    def _auto_detect_host(self):
        try:
            import socket
            fqdn = socket.getfqdn()
            if fqdn and "." in fqdn and fqdn != "localhost" and "localhost" not in fqdn:
                return fqdn
            hostname = socket.gethostname()
            if hostname and hostname != "localhost":
                return hostname
        except Exception:
            pass
        return self.DEFAULT_TRANSFER_HOST

    # ---- Helpers ----
    def _find_hub_plugins_dir(self):
        cwd = os.path.abspath(os.getcwd())
        candidates = [
            os.path.join(cwd, "plugins"),
            os.path.join(os.path.dirname(cwd), "plugins"),
            os.path.join(cwd, "bedrock_server", "plugins"),
        ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return None

    def _find_endstone_executable(self):
        if hasattr(sys, "prefix"):
            candidate = os.path.join(sys.prefix, "bin", "endstone")
            if os.path.isfile(candidate):
                return candidate
        for c in ("/endstone/.venv/bin/endstone", "/usr/local/bin/endstone", "/usr/bin/endstone"):
            if os.path.isfile(c):
                return c
        return "endstone"

    def _resolve_targets(self, sender, target_name):
        target_name = target_name.strip()
        if target_name == "@s":
            return [sender.name]
        if target_name == "@a":
            return [p.name for p in self.server.online_players]
        if target_name == "@e":
            return [p.name for p in self.server.online_players]
        if target_name == "@p":
            return [sender.name]
        if target_name == "@r":
            import random
            online = [p.name for p in self.server.online_players]
            return [random.choice(online)] if online else []
        if target_name.startswith("@"):
            import re
            m = re.search(r"name=([^,\]]+)", target_name)
            if m:
                return [m.group(1).strip("\"' ")]
            return []
        return [target_name]

    def load_data(self):
        if not os.path.exists(self.DATA_FILE):
            return {"worlds": {}, "invites": {}}
        try:
            with open(self.DATA_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load multiworld data: {e}")
            return {"worlds": {}, "invites": {}}

    def save_data(self):
        try:
            with open(self.DATA_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save multiworld data: {e}")

    def _load_data_world(self):
        """World-side data load (returns fresh copy without caching)."""
        if not os.path.exists(self.DATA_FILE):
            return {"worlds": {}, "invites": {}}
        try:
            with open(self.DATA_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load multiworld data: {e}")
            return {"worlds": {}, "invites": {}}

    def _save_data_world(self, data):
        """World-side data save."""
        try:
            with open(self.DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save multiworld data: {e}")

    def _read_messages(self):
        try:
            with open(self.MESSAGES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {"messages": []}

    def _write_messages(self, data):
        try:
            with open(self.MESSAGES_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to write messages: {e}")

    def _send_cross_server_message(self, target_name, message):
        try:
            data = self._read_messages()
            data["messages"].append({
                "target": target_name.lower(),
                "message": message,
                "timestamp": time.time(),
            })
            cutoff = time.time() - 300
            data["messages"] = [m for m in data["messages"] if m.get("timestamp", 0) > cutoff]
            self._write_messages(data)
        except Exception as e:
            self.logger.error(f"Failed to send cross-server message: {e}")

    def _message_poll_loop(self):
        seen_ids = set()
        while True:
            try:
                time.sleep(self.MESSAGE_POLL_INTERVAL)
                data = self._read_messages()
                online = {p.name.lower(): p for p in self.server.online_players}
                for msg in data.get("messages", []):
                    msg_id = f"{msg.get('timestamp')}_{msg.get('target')}_{msg.get('message')}"
                    if msg_id in seen_ids:
                        continue
                    target = msg.get("target", "").lower()
                    if target in online:
                        seen_ids.add(msg_id)
                        player_name = online[target].name
                        message_text = msg.get("message", "")
                        def deliver():
                            try:
                                p = self.server.get_player(player_name)
                                if p:
                                    p.send_message(message_text)
                            except Exception:
                                pass
                        self.server.scheduler.run_task(self, deliver)
            except Exception as e:
                self.logger.warning(f"Message poll error: {e}")

    # ===== PRESENCE TRACKING =====
    def _server_id(self):
        """Return a unique ID for this server (hub uses 'hub', worlds use owner UUID)."""
        if _IS_HUB:
            return "hub"
        cwd = os.path.abspath(os.getcwd())
        # Try to match cwd to a world UUID
        try:
            data = self._load_data_world() if hasattr(self, "_load_data_world") else self.load_data()
            for uuid, world in data.get("worlds", {}).items():
                try:
                    if os.path.abspath(world["path"]) == cwd:
                        return uuid
                except Exception:
                    continue
        except Exception:
            pass
        return f"unknown:{os.path.basename(cwd)}"

    def _read_presence(self):
        try:
            if os.path.exists(self.PRESENCE_FILE):
                with open(self.PRESENCE_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {"servers": {}}

    def _write_presence(self, data):
        try:
            with open(self.PRESENCE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to write presence: {e}")

    def _presence_loop(self):
        """Periodically write our online players list to the shared presence file."""
        my_id = self._server_id()
        while True:
            try:
                time.sleep(self.PRESENCE_UPDATE_INTERVAL)
                try:
                    online = [p.name for p in self.server.online_players]
                except Exception:
                    online = []
                data = self._read_presence()
                data.setdefault("servers", {})[my_id] = {
                    "players": online,
                    "updated_at": time.time(),
                }
                # Drop stale entries (>30s since update)
                cutoff = time.time() - 30
                data["servers"] = {k: v for k, v in data["servers"].items() if v.get("updated_at", 0) > cutoff or k == my_id}
                self._write_presence(data)
            except Exception as e:
                self.logger.warning(f"Presence loop error: {e}")

    def _find_player_world(self, player_name):
        """Find which world a player is currently on.
        Returns (server_id, display_name) tuple or (None, None) if not online anywhere.
        """
        try:
            data = self._read_presence()
            target_lower = player_name.lower()
            for sid, info in data.get("servers", {}).items():
                players = [p.lower() for p in info.get("players", [])]
                if target_lower in players:
                    if sid == "hub":
                        return ("hub", "the hub")
                    # Look up the world's display name from data
                    if _IS_HUB and hasattr(self, "data"):
                        world = self.data.get("worlds", {}).get(sid)
                    else:
                        wd = self._load_data_world() if hasattr(self, "_load_data_world") else self.load_data()
                        world = wd.get("worlds", {}).get(sid)
                    if world:
                        return (sid, world.get("display_name", f"{world.get('owner_name', '?')}'s World"))
                    return (sid, f"world {sid[:8]}")
        except Exception as e:
            self.logger.warning(f"Find player error: {e}")
        return (None, None)

    def get_next_available_port(self):
        used_ports = {w["port"] for w in self.data["worlds"].values()}
        for port in range(self.WORLD_PORT_START, self.WORLD_PORT_END + 1):
            if port not in used_ports:
                return port
        return None

    def _kill_processes_using_path(self, target_path):
        try:
            import psutil
            target_path = os.path.abspath(target_path)
            for proc in psutil.process_iter(["pid", "name", "exe", "cwd"]):
                try:
                    exe = (proc.info.get("exe") or "")
                    cwd = (proc.info.get("cwd") or "")
                    if target_path in exe or target_path in cwd:
                        self.logger.info(f"Killing orphan process: {proc.info['name']} (pid={proc.info['pid']})")
                        proc.kill()
                except Exception:
                    continue
        except Exception as e:
            self.logger.warning(f"Process cleanup error: {e}")

    def _start_world(self, owner_uuid):
        if owner_uuid not in self.data["worlds"]:
            return False
        if owner_uuid in self.running_worlds:
            return True

        world = self.data["worlds"][owner_uuid]
        world_dir = world["path"]
        port = world["port"]

        try:
            self.logger.info(f"Starting world server for {world['owner_name']} on port {port}")
            endstone_exe = self._find_endstone_executable()
            log_file = os.path.join(world_dir, "server.log")
            log_fp = open(log_file, "w")
            # Use PIPE for stdin so bedrock_server doesn't get EOF and shut down.
            process = subprocess.Popen(
                [endstone_exe, "--server-folder", world_dir, "--no-confirm"],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
            )
            self.logger.info(f"World logs writing to: {log_file}")
            self.running_worlds[owner_uuid] = {
                "process": process,
                "port": port,
                "last_active": time.time(),
            }
            # Schedule gamerule push 60 seconds after start (gives world time to fully boot)
            def push_gamerules_later():
                time.sleep(60)
                self._push_gamerules_to_world(owner_uuid)
            Thread(target=push_gamerules_later, daemon=True).start()
            return True
        except Exception as e:
            self.logger.error(f"Failed to start world: {e}")
            return False

    def _push_gamerules_to_world(self, owner_uuid):
        """Send gamerule commands to a world server's stdin to apply runtime settings."""
        if owner_uuid not in self.running_worlds:
            return
        if owner_uuid not in self.data["worlds"]:
            return
        info = self.running_worlds[owner_uuid]
        world = self.data["worlds"][owner_uuid]
        process = info.get("process")
        if not process or not process.stdin:
            return

        commands = []
        # Gamerule mappings
        commands.append(f"gamerule doDaylightCycle {'true' if world.get('day_night_cycle', True) else 'false'}")
        commands.append(f"gamerule doWeatherCycle {'true' if world.get('weather_cycle', True) else 'false'}")
        commands.append(f"gamerule doMobSpawning {'true' if world.get('mob_spawning', True) else 'false'}")
        commands.append(f"gamerule pvp {'true' if world.get('pvp', True) else 'false'}")
        commands.append(f"gamerule keepInventory {'true' if world.get('keep_inventory', False) else 'false'}")

        # Time lock
        locked_time = world.get("locked_time")
        if locked_time is not None:
            if isinstance(locked_time, int):
                commands.append(f"time set {locked_time}")
            else:
                commands.append(f"time set {locked_time}")
            commands.append("gamerule doDaylightCycle false")

        # Weather lock
        locked_weather = world.get("locked_weather")
        if locked_weather:
            commands.append(f"weather {locked_weather}")
            commands.append("gamerule doWeatherCycle false")

        try:
            for cmd in commands:
                process.stdin.write((cmd + "\n").encode())
            process.stdin.flush()
            self.logger.info(f"Pushed {len(commands)} gamerule commands to world {owner_uuid}")
        except Exception as e:
            self.logger.warning(f"Failed to push gamerules: {e}")

    def _stop_world(self, owner_uuid):
        if owner_uuid not in self.running_worlds:
            return
        info = self.running_worlds[owner_uuid]
        try:
            import psutil
            parent = psutil.Process(info["process"].pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            parent.kill()
            info["process"].wait(timeout=10)
        except Exception as e:
            self.logger.warning(f"Error stopping world: {e}")
            try:
                info["process"].kill()
            except Exception:
                pass
        del self.running_worlds[owner_uuid]
        self.logger.info(f"Stopped world server for {owner_uuid}")

    def _cleanup_loop(self):
        while True:
            try:
                time.sleep(60)
                now = time.time()
                for owner_uuid, info in list(self.running_worlds.items()):
                    # Use per-world idle timeout if set, else default
                    world = self.data["worlds"].get(owner_uuid, {})
                    timeout_minutes = world.get("idle_timeout_minutes", 5)
                    timeout_seconds = max(60, timeout_minutes * 60)  # min 1 minute
                    if now - info["last_active"] > timeout_seconds:
                        self.logger.info(f"World {owner_uuid} idle, shutting down to save RAM")
                        self._stop_world(owner_uuid)
            except Exception as e:
                self.logger.error(f"Cleanup loop error: {e}")

    # ---- Command dispatcher ----
    def on_command(self, sender: CommandSender, command: Command, args):
        if not isinstance(sender, Player):
            sender.send_message(f"{ColorFormat.RED}This command must be used by a player.")
            return True

        if len(args) == 0:
            # Open the UI menu instead of showing text help
            if _IS_HUB:
                self._open_main_menu_hub(sender)
            else:
                self._open_main_menu_world(sender)
            return True

        action = args[0].lower()

        if _IS_HUB:
            return self._dispatch_hub(sender, action, args)
        else:
            return self._dispatch_world(sender, action, args)

    def _cmd_locate(self, sender, target_name):
        targets = self._resolve_targets(sender, target_name) if hasattr(self, "_resolve_targets") else [target_name]
        if not targets:
            sender.send_message(f"{ColorFormat.RED}No matching player found for '{target_name}'.")
            return
        for name in targets:
            sid, display = self._find_player_world(name)
            if display:
                sender.send_message(f"{ColorFormat.GREEN}{name} is on {ColorFormat.YELLOW}{display}{ColorFormat.GREEN}.")
            else:
                sender.send_message(f"{ColorFormat.GRAY}{name} is not online anywhere.")

    def _dispatch_hub(self, sender, action, args):
        # Special: /world with no args opens the UI menu
        # (handled before this in on_command, but we keep text fallback below)
        if action == "menu":
            self._open_main_menu_hub(sender)
            return True
        if action == "create":
            if len(args) < 3:
                # If they just typed /world create open the UI
                self._open_create_form(sender)
                return True
            self._cmd_create(sender, args[1].lower(), args[2].lower())
        elif action == "delete":
            self._cmd_delete_confirm(sender)
        elif action == "invite":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world invite <player>")
                return True
            self._cmd_invite_hub(sender, args[1])
        elif action == "uninvite":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world uninvite <player>")
                return True
            self._cmd_uninvite_hub(sender, args[1])
        elif action == "join":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world join <player>")
                return True
            self._cmd_join(sender, args[1])
        elif action == "leave":
            self._cmd_leave_hub(sender)
        elif action == "list":
            self._cmd_list_hub(sender)
        elif action == "browse":
            self._open_browse_menu(sender)
        elif action == "locate":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world locate <player>")
                return True
            self._cmd_locate(sender, args[1])
        elif action == "like":
            self._cmd_like(sender, args[1] if len(args) > 1 else None)
        elif action == "dislike":
            self._cmd_dislike(sender, args[1] if len(args) > 1 else None)
        elif action == "profile":
            target = args[1] if len(args) > 1 else sender.name
            self._open_profile_menu(sender, target)
        elif action == "friends":
            self._open_friends_menu(sender)
        elif action == "achievements":
            self._open_achievements_menu(sender)
        elif action == "leaderboard":
            self._open_leaderboard_menu(sender)
        elif action == "template":
            self._open_templates_menu(sender)
        elif action == "clone":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world clone <owner>")
                return True
            self._cmd_clone(sender, args[1])
        else:
            self._show_help(sender)
        return True

    def _dispatch_world(self, sender, action, args):
        if action == "leave":
            self._cmd_leave_world(sender)
        elif action == "list":
            self._cmd_list_world(sender)
        elif action == "browse":
            self._open_browse_menu(sender)
        elif action == "locate":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world locate <player>")
                return True
            self._cmd_locate(sender, args[1])
        elif action == "invite":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world invite <player>")
                return True
            self._cmd_invite_world(sender, args[1])
        elif action == "uninvite":
            if len(args) < 2:
                sender.send_message(f"{ColorFormat.RED}Usage: /world uninvite <player>")
                return True
            self._cmd_uninvite_world(sender, args[1])
        elif action == "like":
            self._cmd_like(sender, args[1] if len(args) > 1 else None)
        elif action == "dislike":
            self._cmd_dislike(sender, args[1] if len(args) > 1 else None)
        elif action == "profile":
            target = args[1] if len(args) > 1 else sender.name
            self._open_profile_menu(sender, target)
        elif action == "friends":
            self._open_friends_menu(sender)
        elif action == "achievements":
            self._open_achievements_menu(sender)
        elif action == "leaderboard":
            self._open_leaderboard_menu(sender)
        elif action in ("create", "delete", "join", "template", "clone"):
            sender.send_message(f"{ColorFormat.RED}You can only do that from the hub. Use /world leave first.")
        else:
            self._show_help(sender)
        return True

    def _show_help(self, sender):
        sender.send_message(f"{ColorFormat.GOLD}===== MultiWorld Commands =====")
        if _IS_HUB:
            sender.send_message(f"{ColorFormat.YELLOW}/world create <normal|flat> <survival|creative|adventure> {ColorFormat.WHITE}- Create your world")
            sender.send_message(f"{ColorFormat.YELLOW}/world delete {ColorFormat.WHITE}- Delete your world")
            sender.send_message(f"{ColorFormat.YELLOW}/world invite <player> {ColorFormat.WHITE}- Invite someone to your world")
            sender.send_message(f"{ColorFormat.YELLOW}/world uninvite <player> {ColorFormat.WHITE}- Remove a player's invite")
            sender.send_message(f"{ColorFormat.YELLOW}/world join <player> {ColorFormat.WHITE}- Join a world you've been invited to")
            sender.send_message(f"{ColorFormat.YELLOW}/world leave {ColorFormat.WHITE}- Return to the hub")
            sender.send_message(f"{ColorFormat.YELLOW}/world list {ColorFormat.WHITE}- See your invites")
        else:
            sender.send_message(f"{ColorFormat.YELLOW}/world leave {ColorFormat.WHITE}- Return to the hub")
            sender.send_message(f"{ColorFormat.YELLOW}/world list {ColorFormat.WHITE}- See your invites")
            sender.send_message(f"{ColorFormat.YELLOW}/world invite <player> {ColorFormat.WHITE}- Invite someone to this world")
            sender.send_message(f"{ColorFormat.YELLOW}/world uninvite <player> {ColorFormat.WHITE}- Remove a player's invite")

    # ===== HUB-side commands =====
    # ===== UI MENUS (HUB) =====
    def _open_main_menu_hub(self, sender):
        owner_uuid = str(sender.unique_id)
        has_world = owner_uuid in self.data["worlds"]

        invited_count = 0
        for uuid, world in self.data["worlds"].items():
            allowed_by_uuid = owner_uuid in world.get("allowed_players", [])
            allowed_by_name = sender.name.lower() in [n.lower() for n in world.get("name_invites", [])]
            if (allowed_by_uuid or allowed_by_name) and uuid != owner_uuid:
                invited_count += 1

        if has_world:
            w = self.data["worlds"][owner_uuid]
            content = f"§aYou have a world ({w.get('world_type', 'normal')}, {w.get('gamemode', 'survival')})\n§7Invites: {invited_count}"
        else:
            content = f"§7You don't have a world yet.\n§7Invites: {invited_count}"

        form = ActionForm(
            title="§l§bMultiWorld",
            content=content,
        )

        if has_world:
            form.add_button("§aJoin My World", on_click=lambda p: self._cmd_join(p, p.name))
            form.add_button("§eMy World Settings", on_click=lambda p: self._open_my_world_menu(p))
        else:
            form.add_button("§aCreate World", on_click=lambda p: self._open_create_form(p))
            form.add_button("§3Clone From Template", on_click=lambda p: self._open_templates_menu(p))

        if invited_count > 0:
            form.add_button("§bJoin Invite", on_click=lambda p: self._open_join_invite_menu(p))

        form.add_button("§dBrowse Worlds", on_click=lambda p: self._open_browse_menu(p))
        form.add_button("§b🔍 Locate Player", on_click=lambda p: self._open_locate_form(p))
        form.add_button("§eMy Profile", on_click=lambda p, n=sender.name: self._open_profile_menu(p, n))
        form.add_button("§aFriends", on_click=lambda p: self._open_friends_menu(p))
        form.add_button("§6Achievements", on_click=lambda p: self._open_achievements_menu(p))
        form.add_button("§5Leaderboards", on_click=lambda p: self._open_leaderboard_menu(p))
        form.add_button("§7List Worlds", on_click=lambda p: self._cmd_list_hub(p))
        form.add_button("§cClose", on_click=lambda p: None)
        sender.send_form(form)

    def _open_my_world_menu(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You don't have a world.")
            return

        world = self.data["worlds"][owner_uuid]
        invites = world.get("name_invites", [])
        invite_list = "\n".join(f"§7- {n}" for n in invites) if invites else "§7(none)"
        display_name = world.get("display_name", f"{world['owner_name']}'s World")
        category = world.get("category", "Other")
        description = world.get("description", "")
        public_str = "§aPublic" if world.get("public", False) else "§ePrivate (invite only)"
        likes = len(world.get("likes", []))
        dislikes = len(world.get("dislikes", []))
        content = (
            f"§a§l{display_name}\n"
            f"§7Category: §f{category}    §7| §f{public_str}\n"
            f"§7Type: §f{world.get('world_type', 'normal')}, {world.get('gamemode', 'survival')}\n"
            f"§7Port: §f{world['port']}    §7| §a▲{likes} §c▼{dislikes}\n"
        )
        if description:
            content += f"\n§e\"{description}\"\n"
        content += f"\n§eInvited:\n{invite_list}"

        form = ActionForm(
            title="§l§bMy World",
            content=content,
        )
        form.add_button("§aInvite Player", on_click=lambda p: self._open_invite_form(p))
        form.add_button("§eUninvite Player", on_click=lambda p: self._open_uninvite_form(p))
        form.add_button("§b⚙ Settings", on_click=lambda p: self._open_settings_menu(p))
        form.add_button("§cDelete World", on_click=lambda p: self._open_delete_confirm(p))
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p))
        sender.send_form(form)

    # ===== SETTINGS UI =====
    def _open_settings_menu(self, sender):
        """Settings hub with categories."""
        form = ActionForm(
            title="§l§bWorld Settings",
            content="§7Pick a category to edit.",
        )
        form.add_button("§aIdentity\n§7Name, description, category, icon", on_click=lambda p: self._open_settings_identity(p))
        form.add_button("§3World Mode\n§7Play / Build / Dev", on_click=lambda p: self._open_settings_mode(p))
        form.add_button("§eGameplay\n§7Difficulty, distances, max players", on_click=lambda p: self._open_settings_gameplay(p))
        form.add_button("§dGame Rules\n§7PVP, mobs, weather, time...", on_click=lambda p: self._open_settings_rules(p))
        form.add_button("§6Time & Weather Lock\n§7Always day, always sunny, etc.", on_click=lambda p: self._open_settings_timeweather(p))
        form.add_button("§bPrivacy\n§7Public or invite-only", on_click=lambda p: self._open_settings_privacy(p))
        form.add_button("§5Player Limits\n§7Locked gamemode, owner-only build", on_click=lambda p: self._open_settings_limits(p))
        form.add_button("§2Player Permissions\n§7Per-player build/command access", on_click=lambda p: self._open_settings_player_perms(p))
        form.add_button("§9Plugin\n§7Idle timeout", on_click=lambda p: self._open_settings_plugin(p))
        form.add_button("§7Back", on_click=lambda p: self._open_my_world_menu(p))
        sender.send_form(form)

    def _open_settings_identity(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        categories = ["Survival", "Creative", "Adventure", "Mini-game", "Build", "Parkour", "PvP", "Roleplay", "Hide & Seek", "Bedwars", "Skywars", "TNT Run", "Spleef", "Capture The Flag", "KitPvP", "Bridges", "Modern", "Medieval", "Sci-fi", "Fantasy", "SMP", "Hardcore", "Skyblock", "Prison", "Factions", "Economy", "Music", "Art", "Education", "Showcase", "Test", "Empty", "Other"]
        current_cat = w.get("category", "Other")
        cat_idx = categories.index(current_cat) if current_cat in categories else len(categories) - 1

        form = ModalForm(
            title="§l§aIdentity",
            controls=[
                TextInput(label="World Name", placeholder="My Cool World", default_value=w.get("display_name", "")),
                TextInput(label="Description", placeholder="A short description...", default_value=w.get("description", "")),
                Dropdown(label="Category", options=categories, default_index=cat_idx),
                TextInput(label="Icon (emoji or text)", placeholder="🌍", default_value=w.get("icon", "")),
            ],
            on_submit=self._on_settings_identity_submit,
        )
        sender.send_form(form)

    def _on_settings_identity_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            categories = ["Survival", "Creative", "Adventure", "Mini-game", "Build", "Parkour", "PvP", "Roleplay", "Hide & Seek", "Bedwars", "Skywars", "TNT Run", "Spleef", "Capture The Flag", "KitPvP", "Bridges", "Modern", "Medieval", "Sci-fi", "Fantasy", "SMP", "Hardcore", "Skyblock", "Prison", "Factions", "Economy", "Music", "Art", "Education", "Showcase", "Test", "Empty", "Other"]
            w["display_name"] = (v[0] or w.get("display_name", "")).strip()[:40] or f"{player.name}'s World"
            w["description"] = (v[1] or "").strip()[:200]
            w["category"] = categories[v[2]]
            w["icon"] = (v[3] or "").strip()[:8]
            self.save_data()
            player.send_message(f"{ColorFormat.GREEN}Identity updated!")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_settings_gameplay(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        difficulties = ["peaceful", "easy", "normal", "hard"]
        diff_idx = difficulties.index(w.get("difficulty", "easy")) if w.get("difficulty", "easy") in difficulties else 1

        form = ModalForm(
            title="§l§eGameplay",
            controls=[
                Dropdown(label="Difficulty", options=difficulties, default_index=diff_idx),
                TextInput(label="Max Players (1-100)", default_value=str(w.get("max_players", 10))),
                TextInput(label="View Distance (4-32)", default_value=str(w.get("view_distance", 10))),
                TextInput(label="Tick Distance (4-12)", default_value=str(w.get("tick_distance", 4))),
                Label(text="§7Note: Difficulty applies on next world start. Player/distance settings need restart."),
            ],
            on_submit=self._on_settings_gameplay_submit,
        )
        sender.send_form(form)

    def _on_settings_gameplay_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            difficulties = ["peaceful", "easy", "normal", "hard"]
            w["difficulty"] = difficulties[v[0]]
            try:
                w["max_players"] = max(1, min(100, int(v[1])))
            except Exception:
                pass
            try:
                w["view_distance"] = max(4, min(32, int(v[2])))
            except Exception:
                pass
            try:
                w["tick_distance"] = max(4, min(12, int(v[3])))
            except Exception:
                pass
            self.save_data()
            self._rewrite_world_properties(owner_uuid)
            player.send_message(f"{ColorFormat.GREEN}Gameplay updated! Restart your world for changes to apply.")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_settings_rules(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]

        form = ModalForm(
            title="§l§dGame Rules",
            controls=[
                Toggle(label="Day/Night Cycle", default_value=w.get("day_night_cycle", True)),
                Toggle(label="Weather Cycle", default_value=w.get("weather_cycle", True)),
                Toggle(label="Mob Spawning", default_value=w.get("mob_spawning", True)),
                Toggle(label="PVP", default_value=w.get("pvp", True)),
                Toggle(label="Keep Inventory on Death", default_value=w.get("keep_inventory", False)),
                Label(text="§7These apply when the world is running."),
            ],
            on_submit=self._on_settings_rules_submit,
        )
        sender.send_form(form)

    def _on_settings_rules_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            w["day_night_cycle"] = bool(v[0])
            w["weather_cycle"] = bool(v[1])
            w["mob_spawning"] = bool(v[2])
            w["pvp"] = bool(v[3])
            w["keep_inventory"] = bool(v[4])
            self.save_data()
            # Apply now if running
            if owner_uuid in self.running_worlds:
                self._push_gamerules_to_world(owner_uuid)
                player.send_message(f"{ColorFormat.GREEN}Game rules updated and applied!")
            else:
                player.send_message(f"{ColorFormat.GREEN}Game rules saved. They'll apply next time the world starts.")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_settings_timeweather(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        time_options = ["No lock", "Always Day", "Always Noon", "Always Night", "Always Midnight"]
        weather_options = ["No lock", "Always Clear", "Always Rain", "Always Thunder"]

        time_map = {None: 0, "day": 1, "noon": 2, "night": 3, "midnight": 4}
        weather_map = {None: 0, "clear": 1, "rain": 2, "thunder": 3}

        form = ModalForm(
            title="§l§6Time & Weather Lock",
            controls=[
                Dropdown(label="Time Lock", options=time_options, default_index=time_map.get(w.get("locked_time"), 0)),
                Dropdown(label="Weather Lock", options=weather_options, default_index=weather_map.get(w.get("locked_weather"), 0)),
                Label(text="§7Locking disables the natural cycle."),
            ],
            on_submit=self._on_settings_timeweather_submit,
        )
        sender.send_form(form)

    def _on_settings_timeweather_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            time_values = [None, "day", "noon", "night", "midnight"]
            weather_values = [None, "clear", "rain", "thunder"]
            w["locked_time"] = time_values[v[0]]
            w["locked_weather"] = weather_values[v[1]]
            self.save_data()
            if owner_uuid in self.running_worlds:
                self._push_gamerules_to_world(owner_uuid)
                player.send_message(f"{ColorFormat.GREEN}Time/weather locks applied!")
            else:
                player.send_message(f"{ColorFormat.GREEN}Saved. Will apply on next world start.")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_settings_privacy(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        form = ModalForm(
            title="§l§bPrivacy",
            controls=[
                Toggle(label="Public (anyone can join without invite)", default_value=w.get("public", False)),
                Label(text="§7Private worlds require an invite to join."),
            ],
            on_submit=self._on_settings_privacy_submit,
        )
        sender.send_form(form)

    def _on_settings_privacy_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            w["public"] = bool(v[0])
            self.save_data()
            player.send_message(f"{ColorFormat.GREEN}Privacy updated! Now {'public' if w['public'] else 'private'}.")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_settings_limits(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        form = ModalForm(
            title="§l§5Player Limits",
            controls=[
                Toggle(label="Lock Gamemode (players can't /gamemode)", default_value=w.get("locked_gamemode", False)),
                Toggle(label="Owner-Only Build (only owner can break/place blocks)", default_value=w.get("owner_only_build", False)),
                TextInput(label="Spawn Protection Radius (0 = off)", default_value=str(w.get("spawn_protection_radius", 0))),
            ],
            on_submit=self._on_settings_limits_submit,
        )
        sender.send_form(form)

    def _on_settings_limits_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            w["locked_gamemode"] = bool(v[0])
            w["owner_only_build"] = bool(v[1])
            try:
                w["spawn_protection_radius"] = max(0, min(64, int(v[2])))
            except Exception:
                pass
            self.save_data()
            player.send_message(f"{ColorFormat.GREEN}Player limits updated!")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_settings_plugin(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        form = ModalForm(
            title="§l§9Plugin Settings",
            controls=[
                TextInput(label="Idle Timeout in Minutes (1-60)", default_value=str(w.get("idle_timeout_minutes", 5))),
                Label(text="§7How long an empty world stays running before auto-shutdown."),
            ],
            on_submit=self._on_settings_plugin_submit,
        )
        sender.send_form(form)

    def _on_settings_plugin_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            try:
                w["idle_timeout_minutes"] = max(1, min(60, int(v[0])))
            except Exception:
                pass
            self.save_data()
            player.send_message(f"{ColorFormat.GREEN}Plugin settings updated!")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _rewrite_world_properties(self, owner_uuid):
        """Rewrite server.properties for a world after settings change."""
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        try:
            self._write_world_properties(
                w["path"], w["port"], w["owner_name"],
                w["world_type"], w["gamemode"], world_data=w
            )
        except Exception as e:
            self.logger.warning(f"Failed to rewrite properties: {e}")

    def _open_create_form(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You already have a world! Delete it first to create a new one.")
            return

        form = ModalForm(
            title="§l§aCreate World",
            controls=[
                Dropdown(label="World Type", options=["Normal", "Flat"], default_index=0),
                Dropdown(label="Gamemode", options=["Survival", "Creative", "Adventure"], default_index=1),
            ],
            on_submit=self._on_create_submit,
        )
        sender.send_form(form)

    def _on_create_submit(self, player, json_str):
        try:
            values = json.loads(json_str)
            world_type = ["normal", "flat"][values[0]]
            gamemode = ["survival", "creative", "adventure"][values[1]]
            self._cmd_create(player, world_type, gamemode)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed to parse form: {e}")

    def _open_invite_form(self, sender):
        # List online players (excluding self) as a dropdown for easier picking
        online = [p.name for p in self.server.online_players if p.name != sender.name]
        if online:
            form = ModalForm(
                title="§l§aInvite Player",
                controls=[
                    Label(text="Pick from online players or type a name."),
                    Dropdown(label="Online Players", options=["(type below)"] + online, default_index=0),
                    TextInput(label="Or type a name", placeholder="Player name"),
                ],
                on_submit=lambda p, json_str: self._on_invite_submit(p, json_str, online),
            )
        else:
            form = ModalForm(
                title="§l§aInvite Player",
                controls=[
                    TextInput(label="Player name", placeholder="Player name"),
                ],
                on_submit=lambda p, json_str: self._on_invite_submit_text_only(p, json_str),
            )
        sender.send_form(form)

    def _on_invite_submit(self, player, json_str, online):
        try:
            values = json.loads(json_str)
            # values[0] is the label (ignored)
            dropdown_idx = values[1]
            text_input = values[2].strip() if len(values) > 2 else ""
            if dropdown_idx > 0:
                target = online[dropdown_idx - 1]
            elif text_input:
                target = text_input
            else:
                player.send_message(f"{ColorFormat.RED}Please select a player or type a name.")
                return
            self._cmd_invite_hub(player, target)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed to parse form: {e}")

    def _on_invite_submit_text_only(self, player, json_str):
        try:
            values = json.loads(json_str)
            target = values[0].strip()
            if not target:
                player.send_message(f"{ColorFormat.RED}Please type a player name.")
                return
            self._cmd_invite_hub(player, target)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed to parse form: {e}")

    def _open_uninvite_form(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You don't have a world.")
            return
        invites = self.data["worlds"][owner_uuid].get("name_invites", [])
        if not invites:
            sender.send_message(f"{ColorFormat.YELLOW}You haven't invited anyone yet.")
            return

        form = ActionForm(
            title="§l§eUninvite Player",
            content="§7Tap a player to uninvite them.",
        )
        for name in invites:
            form.add_button(f"§c{name}", on_click=lambda p, n=name: self._cmd_uninvite_hub(p, n))
        form.add_button("§7Back", on_click=lambda p: self._open_my_world_menu(p))
        sender.send_form(form)

    def _open_delete_confirm(self, sender):
        form = MessageForm(
            title="§l§cDelete World?",
            content="§eAre you sure? §7This cannot be undone.",
            button1="§cYes, Delete",
            button2="§7Cancel",
            on_submit=self._on_delete_confirm,
        )
        sender.send_form(form)

    def _on_delete_confirm(self, player, button_index):
        # button_index 0 = first button (Yes), 1 = second (Cancel)
        if button_index == 0:
            self._cmd_delete(player)
        else:
            self._open_my_world_menu(player)

    def _cmd_delete_confirm(self, sender):
        # Used when user types /world delete from chat
        self._open_delete_confirm(sender)

    def _open_join_invite_menu(self, sender):
        sender_uuid = str(sender.unique_id)
        invited_to = []
        for uuid, world in self.data["worlds"].items():
            allowed_by_uuid = sender_uuid in world.get("allowed_players", [])
            allowed_by_name = sender.name.lower() in [n.lower() for n in world.get("name_invites", [])]
            if (allowed_by_uuid or allowed_by_name) and uuid != sender_uuid:
                invited_to.append(world["owner_name"])

        if not invited_to:
            sender.send_message(f"{ColorFormat.YELLOW}You haven't been invited to any worlds.")
            return

        form = ActionForm(
            title="§l§bJoin Invite",
            content="§7Tap a world to join.",
        )
        for name in invited_to:
            form.add_button(f"§a{name}'s World", on_click=lambda p, n=name: self._cmd_join(p, n))
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p))
        sender.send_form(form)

    # ===== UI MENUS (WORLD) =====
    def _open_main_menu_world(self, sender):
        data = self._load_data_world()
        sender_uuid = str(sender.unique_id)
        # Determine if sender is the owner of this world
        cwd = os.path.abspath(os.getcwd())
        world_owner_uuid = None
        for uuid, world in data["worlds"].items():
            try:
                if os.path.abspath(world["path"]) == cwd:
                    world_owner_uuid = uuid
                    break
            except Exception:
                continue
        is_owner = (world_owner_uuid == sender_uuid)

        invited_count = 0
        for uuid, world in data["worlds"].items():
            allowed_by_uuid = sender_uuid in world.get("allowed_players", [])
            allowed_by_name = sender.name.lower() in [n.lower() for n in world.get("name_invites", [])]
            if (allowed_by_uuid or allowed_by_name) and uuid != sender_uuid:
                invited_count += 1

        content = f"§7Invites: {invited_count}\n§7You are {'the owner' if is_owner else 'a guest'} here."

        form = ActionForm(
            title="§l§bMultiWorld",
            content=content,
        )
        form.add_button("§aLeave (Back to Hub)", on_click=lambda p: self._cmd_leave_world(p))
        if is_owner:
            form.add_button("§bInvite Player", on_click=lambda p: self._open_invite_form_world(p))
            form.add_button("§eUninvite Player", on_click=lambda p: self._open_uninvite_form_world(p))
        else:
            form.add_button("§a▲ Like This World", on_click=lambda p: self._cmd_like(p, None))
            form.add_button("§c▼ Dislike This World", on_click=lambda p: self._cmd_dislike(p, None))
        form.add_button("§dBrowse Worlds", on_click=lambda p: self._open_browse_menu(p))
        form.add_button("§b🔍 Locate Player", on_click=lambda p: self._open_locate_form(p))
        form.add_button("§eMy Profile", on_click=lambda p, n=sender.name: self._open_profile_menu(p, n))
        form.add_button("§aFriends", on_click=lambda p: self._open_friends_menu(p))
        form.add_button("§6Achievements", on_click=lambda p: self._open_achievements_menu(p))
        form.add_button("§5Leaderboards", on_click=lambda p: self._open_leaderboard_menu(p))
        form.add_button("§7List Worlds", on_click=lambda p: self._cmd_list_world(p))
        form.add_button("§cClose", on_click=lambda p: None)
        sender.send_form(form)

    def _open_invite_form_world(self, sender):
        online = [p.name for p in self.server.online_players if p.name != sender.name]
        if online:
            form = ModalForm(
                title="§l§aInvite Player",
                controls=[
                    Label(text="Pick from online players or type a name."),
                    Dropdown(label="Online Players", options=["(type below)"] + online, default_index=0),
                    TextInput(label="Or type a name", placeholder="Player name"),
                ],
                on_submit=lambda p, json_str: self._on_invite_submit_world(p, json_str, online),
            )
        else:
            form = ModalForm(
                title="§l§aInvite Player",
                controls=[
                    TextInput(label="Player name", placeholder="Player name"),
                ],
                on_submit=lambda p, json_str: self._on_invite_submit_world_text_only(p, json_str),
            )
        sender.send_form(form)

    def _on_invite_submit_world(self, player, json_str, online):
        try:
            values = json.loads(json_str)
            dropdown_idx = values[1]
            text_input = values[2].strip() if len(values) > 2 else ""
            if dropdown_idx > 0:
                target = online[dropdown_idx - 1]
            elif text_input:
                target = text_input
            else:
                player.send_message(f"{ColorFormat.RED}Please select a player or type a name.")
                return
            self._cmd_invite_world(player, target)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed to parse form: {e}")

    def _on_invite_submit_world_text_only(self, player, json_str):
        try:
            values = json.loads(json_str)
            target = values[0].strip()
            if not target:
                player.send_message(f"{ColorFormat.RED}Please type a player name.")
                return
            self._cmd_invite_world(player, target)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed to parse form: {e}")

    def _open_uninvite_form_world(self, sender):
        data = self._load_data_world()
        cwd = os.path.abspath(os.getcwd())
        world_owner_uuid = None
        for uuid, world in data["worlds"].items():
            try:
                if os.path.abspath(world["path"]) == cwd:
                    world_owner_uuid = uuid
                    break
            except Exception:
                continue
        if not world_owner_uuid:
            sender.send_message(f"{ColorFormat.RED}Could not figure out which world you're in.")
            return
        invites = data["worlds"][world_owner_uuid].get("name_invites", [])
        if not invites:
            sender.send_message(f"{ColorFormat.YELLOW}You haven't invited anyone yet.")
            return

        form = ActionForm(
            title="§l§eUninvite Player",
            content="§7Tap a player to uninvite them.",
        )
        for name in invites:
            form.add_button(f"§c{name}", on_click=lambda p, n=name: self._cmd_uninvite_world(p, n))
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_world(p))
        sender.send_form(form)

    def _cmd_create(self, sender, world_type, gamemode):
        owner_uuid = str(sender.unique_id)

        if world_type not in ("normal", "flat"):
            sender.send_message(f"{ColorFormat.RED}World type must be 'normal' or 'flat'.")
            return

        if gamemode not in ("survival", "creative", "adventure"):
            sender.send_message(f"{ColorFormat.RED}Gamemode must be 'survival', 'creative', or 'adventure'.")
            return

        if owner_uuid in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You already have a world! Use /world delete first if you want a new one.")
            return

        port = self.get_next_available_port()
        if port is None:
            sender.send_message(f"{ColorFormat.RED}Sorry, the server has reached the maximum number of worlds.")
            return

        world_dir = os.path.join(self.WORLDS_BASE_DIR, owner_uuid)
        # Cancel any pending background delete for this path
        if hasattr(self, "_pending_deletes"):
            self._pending_deletes.discard(world_dir)
        try:
            os.makedirs(world_dir, exist_ok=True)
            self._write_world_properties(world_dir, port, sender.name, world_type, gamemode)
            self._write_ops_file(world_dir, sender.name, str(sender.xuid))
            Thread(target=self._copy_plugins_to_world, args=(world_dir,), daemon=True).start()
        except Exception as e:
            sender.send_message(f"{ColorFormat.RED}Failed to create world: {e}")
            return

        self.data["worlds"][owner_uuid] = {
            "owner_name": sender.name,
            "port": port,
            "path": world_dir,
            "allowed_players": [owner_uuid],
            "name_invites": [],
            "world_type": world_type,
            "gamemode": gamemode,
            # New v0.9 settings - identity
            "display_name": f"{sender.name}'s World",
            "description": "",
            "category": "Other",
            "icon": "",  # emoji
            # Gameplay
            "difficulty": "easy",
            "max_players": 10,
            "view_distance": 10,
            "tick_distance": 4,
            "day_night_cycle": True,
            "weather_cycle": True,
            "mob_spawning": True,
            "pvp": True,
            "keep_inventory": False,
            # Privacy
            "public": False,  # if True, anyone can join without invite
            # Spawn
            "spawn_protection_radius": 0,
            # Time/weather lock
            "locked_time": None,  # None or "day", "night", "noon", "midnight", or int 0-24000
            "locked_weather": None,  # None or "clear", "rain", "thunder"
            # Player limits
            "locked_gamemode": False,  # if True, players can't change gamemode
            "owner_only_build": False,
            # Plugin behavior
            "idle_timeout_minutes": 5,
            # Stats
            "created_at": time.time(),
            "last_played_at": time.time(),
            "play_count": 0,  # incremented each join
            # Reputation
            "likes": [],  # list of player UUIDs
            "dislikes": [],
            # Phase 4: World mode
            "mode": "play",  # "play", "build", or "dev"
            # Phase 6: Per-player permissions inside this world
            # { uuid: { "build": True, "use_commands": False, "is_friend": False, ... } }
            "player_perms": {},
            # Phase 7: Visual customization
            "banner_color": "blue",
            # Phase 7: Achievements & leaderboard tracking
            "achievements_unlocked": [],  # list of achievement IDs
        }
        self.save_data()

        # Achievement
        self._maybe_unlock_achievements(owner_uuid, "create_world")

        sender.send_message(f"{ColorFormat.GREEN}World created! ({world_type}, {gamemode})")
        sender.send_message(f"{ColorFormat.GREEN}You'll have OP permissions in your world.")
        sender.send_message(f"{ColorFormat.GREEN}Use {ColorFormat.YELLOW}/world join {sender.name}{ColorFormat.GREEN} to enter it.")

    def _copy_plugins_to_world(self, world_dir):
        try:
            world_plugins = os.path.join(world_dir, "plugins")
            os.makedirs(world_plugins, exist_ok=True)
            hub_plugins = self._find_hub_plugins_dir()
            if not hub_plugins:
                self.logger.warning("Could not auto-detect hub plugins folder; skipping plugin copy.")
                return
            self.logger.info(f"Copying plugins from {hub_plugins} -> {world_plugins}")
            for fname in os.listdir(hub_plugins):
                src = os.path.join(hub_plugins, fname)
                if not os.path.isfile(src):
                    continue
                fname_lower = fname.lower()
                # We don't need to skip anything since the same plugin runs in both modes now
                dst = os.path.join(world_plugins, fname)
                try:
                    shutil.copy2(src, dst)
                except Exception as e:
                    self.logger.warning(f"Failed to copy plugin {fname}: {e}")
            self.logger.info(f"Copied plugins to {world_plugins}")
        except Exception as e:
            self.logger.error(f"Plugin copy error: {e}")

    def _write_world_properties(self, world_dir, port, owner_name, world_type, gamemode, world_data=None):
        """Write server.properties. If world_data is provided, use its settings."""
        wd = world_data or {}
        difficulty = wd.get("difficulty", "easy")
        max_players = wd.get("max_players", 10)
        view_distance = wd.get("view_distance", 10)
        tick_distance = wd.get("tick_distance", 4)
        display_name = wd.get("display_name", f"{owner_name}'s World")

        props_path = os.path.join(world_dir, "server.properties")
        with open(props_path, "w") as f:
            f.write(f"server-name={display_name}\n")
            f.write(f"server-port={port}\n")
            f.write(f"server-portv6={port + 1000}\n")
            f.write(f"gamemode={gamemode}\n")
            f.write(f"difficulty={difficulty}\n")
            f.write(f"max-players={max_players}\n")
            f.write("online-mode=true\n")
            f.write("allow-list=false\n")
            f.write("enable-lan-visibility=false\n")
            f.write("level-name=world\n")
            f.write(f"view-distance={view_distance}\n")
            f.write(f"tick-distance={tick_distance}\n")
            if gamemode == "creative":
                f.write("allow-cheats=true\n")
            if world_type == "flat":
                f.write("level-type=FLAT\n")

    def _write_ops_file(self, world_dir, player_name, player_xuid):
        perms_path = os.path.join(world_dir, "permissions.json")
        ops = [
            {
                "permission": "operator",
                "xuid": player_xuid,
            }
        ]
        try:
            with open(perms_path, "w") as f:
                json.dump(ops, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to write permissions.json: {e}")

    def _cmd_delete(self, sender):
        owner_uuid = str(sender.unique_id)

        if owner_uuid not in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You don't have a world.")
            return

        self._stop_world(owner_uuid)

        world_path = self.data["worlds"][owner_uuid]["path"]
        self._kill_processes_using_path(world_path)

        deleted = False
        for attempt in range(5):
            time.sleep(2)
            try:
                shutil.rmtree(world_path, ignore_errors=False)
                deleted = True
                break
            except Exception as e:
                self.logger.warning(f"Delete attempt {attempt + 1} failed: {e}")
                self._kill_processes_using_path(world_path)

        del self.data["worlds"][owner_uuid]
        self.save_data()

        if not deleted:
            world_path_copy = world_path
            self._pending_deletes.add(world_path_copy)
            def background_cleanup():
                for i in range(30):
                    time.sleep(10)
                    # Bail if a new world has been created at this path
                    if world_path_copy not in self._pending_deletes:
                        self.logger.info(f"Background cleanup canceled - new world recreated at {world_path_copy}")
                        return
                    try:
                        self._kill_processes_using_path(world_path_copy)
                        shutil.rmtree(world_path_copy, ignore_errors=False)
                        self._pending_deletes.discard(world_path_copy)
                        self.logger.info(f"Background cleanup deleted: {world_path_copy}")
                        return
                    except Exception:
                        continue
                try:
                    if world_path_copy in self._pending_deletes:
                        shutil.rmtree(world_path_copy, ignore_errors=True)
                        self._pending_deletes.discard(world_path_copy)
                except Exception:
                    pass
            Thread(target=background_cleanup, daemon=True).start()

        sender.send_message(f"{ColorFormat.RED}Your world is no more.")

    def _cmd_invite_hub(self, sender, target_name):
        owner_uuid = str(sender.unique_id)

        if owner_uuid not in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You don't have a world. Create one first with /world create.")
            return

        targets = self._resolve_targets(sender, target_name)
        if not targets:
            sender.send_message(f"{ColorFormat.RED}No matching players found for '{target_name}'.")
            return

        invited, already = [], []
        for name in targets:
            if name.lower() == sender.name.lower():
                continue
            target = self.server.get_player(name)
            target_uuid = str(target.unique_id) if target else None

            world = self.data["worlds"][owner_uuid]
            world.setdefault("name_invites", [])
            world.setdefault("allowed_players", [owner_uuid])

            if name.lower() in [n.lower() for n in world["name_invites"]]:
                already.append(name)
                continue

            if target_uuid and target_uuid not in world["allowed_players"]:
                world["allowed_players"].append(target_uuid)
            world["name_invites"].append(name)
            invited.append(name)
            self._send_cross_server_message(name,
                f"{ColorFormat.GREEN}{sender.name} invited you to their world! Use {ColorFormat.YELLOW}/world join {sender.name}{ColorFormat.GREEN} from the hub to join.")

        if invited:
            self.save_data()
            sender.send_message(f"{ColorFormat.GREEN}Invited: {', '.join(invited)}")
        if already:
            sender.send_message(f"{ColorFormat.YELLOW}Already invited: {', '.join(already)}")
        if not invited and not already:
            sender.send_message(f"{ColorFormat.YELLOW}No one was invited.")

    def _cmd_uninvite_hub(self, sender, target_name):
        owner_uuid = str(sender.unique_id)

        if owner_uuid not in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You don't have a world.")
            return

        targets = self._resolve_targets(sender, target_name)
        if not targets:
            sender.send_message(f"{ColorFormat.RED}No matching players found for '{target_name}'.")
            return

        world = self.data["worlds"][owner_uuid]
        removed, not_invited = [], []
        for name in targets:
            if name.lower() == sender.name.lower():
                continue
            target = self.server.get_player(name)
            target_uuid = str(target.unique_id) if target else None

            removed_anything = False
            name_invites = world.get("name_invites", [])
            new_names = [n for n in name_invites if n.lower() != name.lower()]
            if len(new_names) != len(name_invites):
                world["name_invites"] = new_names
                removed_anything = True
            if target_uuid:
                allowed = world.get("allowed_players", [])
                if target_uuid in allowed and target_uuid != owner_uuid:
                    allowed.remove(target_uuid)
                    removed_anything = True

            if removed_anything:
                removed.append(name)
                self._send_cross_server_message(name,
                    f"{ColorFormat.YELLOW}{sender.name} removed your invite to their world.")
            else:
                not_invited.append(name)

        if removed:
            self.save_data()
            sender.send_message(f"{ColorFormat.GREEN}Removed invites for: {', '.join(removed)}")
        if not_invited:
            sender.send_message(f"{ColorFormat.YELLOW}Not invited: {', '.join(not_invited)}")

    def _cmd_join(self, sender, owner_name):
        resolved = self._resolve_targets(sender, owner_name)
        if not resolved:
            sender.send_message(f"{ColorFormat.RED}No matching player found for '{owner_name}'.")
            return
        owner_name = resolved[0]

        target_world = None
        target_uuid = None
        for uuid, world in self.data["worlds"].items():
            if world["owner_name"].lower() == owner_name.lower():
                target_world = world
                target_uuid = uuid
                break

        if target_world is None:
            sender.send_message(f"{ColorFormat.RED}No world found for {owner_name}.")
            return

        sender_uuid = str(sender.unique_id)
        allowed_by_uuid = sender_uuid in target_world.get("allowed_players", [])
        allowed_by_name = sender.name.lower() in [n.lower() for n in target_world.get("name_invites", [])]
        is_public = target_world.get("public", False)
        is_owner = sender_uuid == target_uuid
        if not (allowed_by_uuid or allowed_by_name or is_public or is_owner):
            sender.send_message(f"{ColorFormat.RED}You're not invited to this world.")
            return

        if allowed_by_name and not allowed_by_uuid:
            target_world.setdefault("allowed_players", []).append(sender_uuid)
            self.save_data()

        if target_uuid not in self.running_worlds:
            sender.send_message(f"{ColorFormat.YELLOW}Starting world... please wait about 40 seconds.")
            if not self._start_world(target_uuid):
                sender.send_message(f"{ColorFormat.RED}Failed to start world. Try again in a moment.")
                return

        self.running_worlds[target_uuid]["last_active"] = time.time()
        target_world["last_played_at"] = time.time()
        target_world["play_count"] = target_world.get("play_count", 0) + 1
        # Track achievements for the visitor
        self._maybe_unlock_achievements(sender_uuid, "visit_world")
        self.save_data()
        port = target_world["port"]
        sender_name = sender.name

        def do_transfer():
            p = self.server.get_player(sender_name)
            if p:
                p.send_message(f"{ColorFormat.GREEN}Connecting to {owner_name}'s world...")
                try:
                    p.transfer(self.transfer_host, port)
                except Exception as e:
                    p.send_message(f"{ColorFormat.RED}Transfer failed: {e}")

        self.server.scheduler.run_task(self, do_transfer, delay=800)

    def _cmd_leave_hub(self, sender):
        try:
            sender.transfer(self.transfer_host, self.hub_port)
            sender.send_message(f"{ColorFormat.GREEN}Returning to hub...")
        except Exception as e:
            sender.send_message(f"{ColorFormat.RED}Transfer failed: {e}")

    def _cmd_list_hub(self, sender):
        sender_uuid = str(sender.unique_id)
        invited_to = []
        for uuid, world in self.data["worlds"].items():
            allowed_by_uuid = sender_uuid in world.get("allowed_players", [])
            allowed_by_name = sender.name.lower() in [n.lower() for n in world.get("name_invites", [])]
            if (allowed_by_uuid or allowed_by_name) and uuid != sender_uuid:
                invited_to.append(world["owner_name"])

        sender.send_message(f"{ColorFormat.GOLD}===== Your Worlds =====")
        if sender_uuid in self.data["worlds"]:
            w = self.data["worlds"][sender_uuid]
            wtype = w.get("world_type", "normal")
            gm = w.get("gamemode", "survival")
            sender.send_message(f"{ColorFormat.GREEN}Your world: port {w['port']} ({wtype}, {gm})")
        else:
            sender.send_message(f"{ColorFormat.GRAY}You don't have a world yet.")

        if invited_to:
            sender.send_message(f"{ColorFormat.GOLD}Invited to:")
            for name in invited_to:
                sender.send_message(f"{ColorFormat.YELLOW}- {name}")
        else:
            sender.send_message(f"{ColorFormat.GRAY}No invites yet.")

    # ===== WORLD-side commands =====
    def _cmd_leave_world(self, sender):
        try:
            sender.transfer(self.transfer_host, self.hub_port)
            sender.send_message(f"{ColorFormat.GREEN}Returning to hub...")
        except Exception as e:
            sender.send_message(f"{ColorFormat.RED}Transfer failed: {e}")

    def _cmd_list_world(self, sender):
        data = self._load_data_world()
        sender_uuid = str(sender.unique_id)
        invited_to = []
        for uuid, world in data["worlds"].items():
            allowed_by_uuid = sender_uuid in world.get("allowed_players", [])
            allowed_by_name = sender.name.lower() in [n.lower() for n in world.get("name_invites", [])]
            if (allowed_by_uuid or allowed_by_name) and uuid != sender_uuid:
                invited_to.append(world["owner_name"])

        sender.send_message(f"{ColorFormat.GOLD}===== Your Worlds =====")
        if sender_uuid in data["worlds"]:
            w = data["worlds"][sender_uuid]
            wtype = w.get("world_type", "normal")
            gm = w.get("gamemode", "survival")
            sender.send_message(f"{ColorFormat.GREEN}Your world: ({wtype}, {gm})")
        else:
            sender.send_message(f"{ColorFormat.GRAY}You don't have a world yet.")

        if invited_to:
            sender.send_message(f"{ColorFormat.GOLD}Invited to:")
            for name in invited_to:
                sender.send_message(f"{ColorFormat.YELLOW}- {name}")
        else:
            sender.send_message(f"{ColorFormat.GRAY}No invites yet.")

    def _find_world_owner_uuid(self):
        cwd = os.path.abspath(os.getcwd())
        data = self._load_data_world()
        for uuid, world in data["worlds"].items():
            try:
                if os.path.abspath(world["path"]) == cwd:
                    return uuid, data
            except Exception:
                continue
        return None, data

    def _cmd_invite_world(self, sender, target_name):
        world_owner_uuid, data = self._find_world_owner_uuid()
        if not world_owner_uuid:
            sender.send_message(f"{ColorFormat.RED}Could not figure out which world you're in.")
            return

        sender_uuid = str(sender.unique_id)
        if sender_uuid != world_owner_uuid:
            sender.send_message(f"{ColorFormat.RED}Only the world's owner can invite players.")
            return

        targets = self._resolve_targets(sender, target_name)
        if not targets:
            sender.send_message(f"{ColorFormat.RED}No matching players found for '{target_name}'.")
            return

        world = data["worlds"][world_owner_uuid]
        world.setdefault("name_invites", [])
        world.setdefault("allowed_players", [world_owner_uuid])

        invited, already = [], []
        for name in targets:
            if name.lower() == sender.name.lower():
                continue
            if name.lower() in [n.lower() for n in world["name_invites"]]:
                already.append(name)
                continue
            world["name_invites"].append(name)
            target = self.server.get_player(name)
            if target is not None:
                tuuid = str(target.unique_id)
                if tuuid not in world["allowed_players"]:
                    world["allowed_players"].append(tuuid)
            invited.append(name)
            self._send_cross_server_message(name,
                f"{ColorFormat.GREEN}{sender.name} invited you to their world! Use {ColorFormat.YELLOW}/world join {sender.name}{ColorFormat.GREEN} from the hub to join.")

        if invited:
            self._save_data_world(data)
            sender.send_message(f"{ColorFormat.GREEN}Invited: {', '.join(invited)}")
        if already:
            sender.send_message(f"{ColorFormat.YELLOW}Already invited: {', '.join(already)}")
        if not invited and not already:
            sender.send_message(f"{ColorFormat.YELLOW}No one was invited.")

    def _cmd_uninvite_world(self, sender, target_name):
        world_owner_uuid, data = self._find_world_owner_uuid()
        if not world_owner_uuid:
            sender.send_message(f"{ColorFormat.RED}Could not figure out which world you're in.")
            return

        sender_uuid = str(sender.unique_id)
        if sender_uuid != world_owner_uuid:
            sender.send_message(f"{ColorFormat.RED}Only the world's owner can uninvite players.")
            return

        targets = self._resolve_targets(sender, target_name)
        if not targets:
            sender.send_message(f"{ColorFormat.RED}No matching players found for '{target_name}'.")
            return

        world = data["worlds"][world_owner_uuid]
        removed, not_invited = [], []
        for name in targets:
            if name.lower() == sender.name.lower():
                continue
            target = self.server.get_player(name)
            target_uuid = str(target.unique_id) if target else None

            removed_anything = False
            name_invites = world.get("name_invites", [])
            new_names = [n for n in name_invites if n.lower() != name.lower()]
            if len(new_names) != len(name_invites):
                world["name_invites"] = new_names
                removed_anything = True
            if target_uuid:
                allowed = world.get("allowed_players", [])
                if target_uuid in allowed and target_uuid != world_owner_uuid:
                    allowed.remove(target_uuid)
                    removed_anything = True

            if removed_anything:
                removed.append(name)
                self._send_cross_server_message(name,
                    f"{ColorFormat.YELLOW}{sender.name} removed your invite to their world.")
            else:
                not_invited.append(name)

        if removed:
            self._save_data_world(data)
            sender.send_message(f"{ColorFormat.GREEN}Removed invites for: {', '.join(removed)}")
        if not_invited:
            sender.send_message(f"{ColorFormat.YELLOW}Not invited: {', '.join(not_invited)}")

    # ===== BROWSE / DISCOVERY UI =====
    # State: per-player filter/sort settings. Stored on the plugin object.
    def _get_browse_state(self, sender):
        if not hasattr(self, "_browse_states"):
            self._browse_states = {}
        uuid = str(sender.unique_id)
        if uuid not in self._browse_states:
            self._browse_states[uuid] = {
                "sort": "online_desc",  # default: busiest first
                "category": "All",
                "world_type": "All",
                "gamemode": "All",
                "difficulty": "All",
                "min_players": 0,
                "running_only": False,
                "has_description": False,
                "owner_online": False,
                "name_search": "",
                "owner_search": "",
            }
        return self._browse_states[uuid]

    def _get_worlds_data(self):
        """Get the worlds dict, working in either hub or world mode."""
        if _IS_HUB and hasattr(self, "data"):
            return self.data.get("worlds", {})
        try:
            data = self._load_data_world()
            return data.get("worlds", {})
        except Exception:
            return {}

    def _get_online_count_for_world(self, uuid):
        """Look up how many players are online in the given world's server."""
        try:
            presence = self._read_presence()
            srv = presence.get("servers", {}).get(uuid)
            if srv:
                return len(srv.get("players", []))
        except Exception:
            pass
        return 0

    def _is_owner_online(self, world):
        """Check if the world's owner is online anywhere."""
        try:
            owner_name = world.get("owner_name", "").lower()
            if not owner_name:
                return False
            presence = self._read_presence()
            for sid, info in presence.get("servers", {}).items():
                for p in info.get("players", []):
                    if p.lower() == owner_name:
                        return True
        except Exception:
            pass
        return False

    def _filter_and_sort_worlds(self, sender):
        """Apply the player's current filter/sort settings to the worlds list."""
        state = self._get_browse_state(sender)
        worlds = self._get_worlds_data()
        sender_uuid = str(sender.unique_id)

        results = []
        for uuid, w in worlds.items():
            # Only show worlds the player can actually access:
            # - They own it
            # - They're invited (uuid or name)
            # - It's public
            allowed_by_uuid = sender_uuid in w.get("allowed_players", [])
            allowed_by_name = sender.name.lower() in [n.lower() for n in w.get("name_invites", [])]
            is_public = w.get("public", False)
            is_owner = uuid == sender_uuid
            if not (allowed_by_uuid or allowed_by_name or is_public or is_owner):
                continue

            # Apply filters
            if state["category"] != "All" and w.get("category", "Other") != state["category"]:
                continue
            if state["world_type"] != "All" and w.get("world_type", "normal") != state["world_type"]:
                continue
            if state["gamemode"] != "All" and w.get("gamemode", "survival") != state["gamemode"]:
                continue
            if state["difficulty"] != "All" and w.get("difficulty", "easy") != state["difficulty"]:
                continue
            online_count = self._get_online_count_for_world(uuid)
            if online_count < state["min_players"]:
                continue
            is_running = uuid in getattr(self, "running_worlds", {}) or online_count > 0
            if state["running_only"] and not is_running:
                continue
            if state["has_description"] and not w.get("description", "").strip():
                continue
            if state["owner_online"] and not self._is_owner_online(w):
                continue
            if state["name_search"]:
                ns = state["name_search"].lower()
                if ns not in w.get("display_name", "").lower() and ns not in w.get("owner_name", "").lower():
                    continue
            if state["owner_search"]:
                if state["owner_search"].lower() not in w.get("owner_name", "").lower():
                    continue

            # Build a result tuple with sort info
            results.append({
                "uuid": uuid,
                "world": w,
                "online": online_count,
                "is_running": is_running,
            })

        # Sort
        sort = state["sort"]
        if sort == "online_desc":
            results.sort(key=lambda r: -r["online"])
        elif sort == "online_asc":
            results.sort(key=lambda r: r["online"])
        elif sort == "newest":
            results.sort(key=lambda r: -r["world"].get("created_at", 0))
        elif sort == "oldest":
            results.sort(key=lambda r: r["world"].get("created_at", 0))
        elif sort == "recent_play":
            results.sort(key=lambda r: -r["world"].get("last_played_at", 0))
        elif sort == "dormant":
            results.sort(key=lambda r: r["world"].get("last_played_at", 0))
        elif sort == "name_asc":
            results.sort(key=lambda r: r["world"].get("display_name", "").lower())
        elif sort == "name_desc":
            results.sort(key=lambda r: r["world"].get("display_name", "").lower(), reverse=True)
        elif sort == "most_liked":
            results.sort(key=lambda r: -(len(r["world"].get("likes", [])) - len(r["world"].get("dislikes", []))))
        elif sort == "most_likes_total":
            results.sort(key=lambda r: -len(r["world"].get("likes", [])))
        elif sort == "random":
            import random
            random.shuffle(results)

        return results

    def _open_browse_menu(self, sender):
        """Main browse view — show filtered/sorted worlds as buttons."""
        results = self._filter_and_sort_worlds(sender)
        state = self._get_browse_state(sender)

        # Header with current sort and active filters
        header_parts = [f"§7Sort: §f{self._sort_label(state['sort'])}"]
        active_filters = []
        if state["category"] != "All": active_filters.append(f"cat={state['category']}")
        if state["world_type"] != "All": active_filters.append(f"type={state['world_type']}")
        if state["gamemode"] != "All": active_filters.append(f"mode={state['gamemode']}")
        if state["difficulty"] != "All": active_filters.append(f"diff={state['difficulty']}")
        if state["min_players"] > 0: active_filters.append(f"≥{state['min_players']} players")
        if state["running_only"]: active_filters.append("running")
        if state["has_description"]: active_filters.append("desc")
        if state["owner_online"]: active_filters.append("owner online")
        if state["name_search"]: active_filters.append(f"name~{state['name_search']}")
        if state["owner_search"]: active_filters.append(f"owner~{state['owner_search']}")
        if active_filters:
            header_parts.append(f"§7Filters: §e{', '.join(active_filters)}")
        header_parts.append(f"§7Showing §f{len(results)}§7 worlds")

        form = ActionForm(
            title="§l§dBrowse Worlds",
            content="\n".join(header_parts),
        )
        form.add_button("§b⚙ Filters & Sort", on_click=lambda p: self._open_browse_filters(p))
        form.add_button("§eClear Filters", on_click=lambda p: self._reset_browse_filters(p))

        if not results:
            form.add_button("§7(no worlds match)", on_click=lambda p: self._open_browse_menu(p))
        else:
            for r in results[:30]:  # cap at 30 entries
                w = r["world"]
                name = w.get("display_name", f"{w.get('owner_name', '?')}'s World")
                icon = w.get("icon", "")
                cat = w.get("category", "Other")
                running_marker = "§a●" if r["is_running"] else "§7○"
                owner = w.get("owner_name", "?")
                likes = len(w.get("likes", []))
                dislikes = len(w.get("dislikes", []))
                line2 = f"§7{cat} | {owner} | {running_marker} §7{r['online']} online | §a▲{likes} §c▼{dislikes}"
                label = f"§f{icon} {name}\n{line2}".strip()
                form.add_button(label, on_click=lambda p, u=r["uuid"]: self._open_world_details(p, u))

        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p) if _IS_HUB else self._open_main_menu_world(p))
        sender.send_form(form)

    def _world_browse_join(self, player, owner_name):
        """When clicking a world from the browse menu inside a world server,
        we need to go back to hub first then join. Easiest: just transfer to hub.
        """
        player.send_message(f"§7Returning to hub to join §f{owner_name}§7's world... use §e/world join {owner_name}§7 once you arrive.")
        try:
            player.transfer(self.transfer_host, self.hub_port)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Transfer failed: {e}")

    def _sort_label(self, sort):
        return {
            "online_desc": "Most players",
            "online_asc": "Least players",
            "newest": "Newest",
            "oldest": "Oldest",
            "recent_play": "Recently played",
            "dormant": "Dormant",
            "name_asc": "Name A-Z",
            "name_desc": "Name Z-A",
            "most_liked": "Best rated (likes - dislikes)",
            "most_likes_total": "Most likes",
            "random": "Random",
        }.get(sort, sort)

    def _reset_browse_filters(self, sender):
        if hasattr(self, "_browse_states"):
            self._browse_states.pop(str(sender.unique_id), None)
        self._open_browse_menu(sender)

    def _open_browse_filters(self, sender):
        """ModalForm for editing all the filter and sort options."""
        state = self._get_browse_state(sender)
        sort_options = ["Most players", "Least players", "Newest", "Oldest",
                        "Recently played", "Dormant", "Name A-Z", "Name Z-A",
                        "Best rated (likes - dislikes)", "Most likes", "Random"]
        sort_keys = ["online_desc", "online_asc", "newest", "oldest",
                     "recent_play", "dormant", "name_asc", "name_desc",
                     "most_liked", "most_likes_total", "random"]
        sort_idx = sort_keys.index(state["sort"]) if state["sort"] in sort_keys else 0

        categories = ["All", "Survival", "Creative", "Adventure", "Mini-game", "Build", "Parkour", "PvP", "Roleplay", "Hide & Seek", "Bedwars", "Skywars", "TNT Run", "Spleef", "Capture The Flag", "KitPvP", "Bridges", "Modern", "Medieval", "Sci-fi", "Fantasy", "SMP", "Hardcore", "Skyblock", "Prison", "Factions", "Economy", "Music", "Art", "Education", "Showcase", "Test", "Empty", "Other"]
        cat_idx = categories.index(state["category"]) if state["category"] in categories else 0

        types = ["All", "normal", "flat"]
        type_idx = types.index(state["world_type"]) if state["world_type"] in types else 0

        gamemodes = ["All", "survival", "creative", "adventure"]
        gm_idx = gamemodes.index(state["gamemode"]) if state["gamemode"] in gamemodes else 0

        difficulties = ["All", "peaceful", "easy", "normal", "hard"]
        diff_idx = difficulties.index(state["difficulty"]) if state["difficulty"] in difficulties else 0

        form = ModalForm(
            title="§l§bFilters & Sort",
            controls=[
                Dropdown(label="Sort By", options=sort_options, default_index=sort_idx),
                Dropdown(label="Category", options=categories, default_index=cat_idx),
                Dropdown(label="World Type", options=types, default_index=type_idx),
                Dropdown(label="Gamemode", options=gamemodes, default_index=gm_idx),
                Dropdown(label="Difficulty", options=difficulties, default_index=diff_idx),
                TextInput(label="Min Online Players", default_value=str(state["min_players"])),
                Toggle(label="Currently Running Only", default_value=state["running_only"]),
                Toggle(label="Has Description", default_value=state["has_description"]),
                Toggle(label="Owner Is Online", default_value=state["owner_online"]),
                TextInput(label="Search by Name", placeholder="(empty for any)", default_value=state["name_search"]),
                TextInput(label="Search by Owner", placeholder="(empty for any)", default_value=state["owner_search"]),
            ],
            on_submit=self._on_browse_filters_submit,
        )
        sender.send_form(form)

    def _on_browse_filters_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            state = self._get_browse_state(player)
            sort_keys = ["online_desc", "online_asc", "newest", "oldest",
                         "recent_play", "dormant", "name_asc", "name_desc",
                         "most_liked", "most_likes_total", "random"]
            categories = ["All", "Survival", "Creative", "Adventure", "Mini-game", "Build", "Parkour", "PvP", "Roleplay", "Hide & Seek", "Bedwars", "Skywars", "TNT Run", "Spleef", "Capture The Flag", "KitPvP", "Bridges", "Modern", "Medieval", "Sci-fi", "Fantasy", "SMP", "Hardcore", "Skyblock", "Prison", "Factions", "Economy", "Music", "Art", "Education", "Showcase", "Test", "Empty", "Other"]
            types = ["All", "normal", "flat"]
            gamemodes = ["All", "survival", "creative", "adventure"]
            difficulties = ["All", "peaceful", "easy", "normal", "hard"]
            state["sort"] = sort_keys[v[0]]
            state["category"] = categories[v[1]]
            state["world_type"] = types[v[2]]
            state["gamemode"] = gamemodes[v[3]]
            state["difficulty"] = difficulties[v[4]]
            try:
                state["min_players"] = max(0, int(v[5]))
            except Exception:
                state["min_players"] = 0
            state["running_only"] = bool(v[6])
            state["has_description"] = bool(v[7])
            state["owner_online"] = bool(v[8])
            state["name_search"] = (v[9] or "").strip()
            state["owner_search"] = (v[10] or "").strip()
            player.send_message(f"{ColorFormat.GREEN}Filters applied!")
            self._open_browse_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    # ===== REPUTATION (LIKE / DISLIKE) =====
    def _find_target_world_for_vote(self, sender, owner_name_or_none):
        """Determine which world the player is voting on.
        - If owner_name given, look it up.
        - Otherwise, if the player is currently in a world server, use that world.
        Returns (uuid, world_dict) or (None, None) and shows an error message.
        """
        if _IS_HUB and hasattr(self, "data"):
            worlds = self.data.get("worlds", {})
        else:
            worlds = self._load_data_world().get("worlds", {})

        # If owner specified, look up by name
        if owner_name_or_none:
            for uuid, w in worlds.items():
                if w.get("owner_name", "").lower() == owner_name_or_none.lower():
                    return (uuid, w)
            sender.send_message(f"{ColorFormat.RED}No world found for {owner_name_or_none}.")
            return (None, None)

        # Otherwise, if we're inside a world server, use that one
        if not _IS_HUB:
            cwd = os.path.abspath(os.getcwd())
            for uuid, w in worlds.items():
                try:
                    if os.path.abspath(w["path"]) == cwd:
                        return (uuid, w)
                except Exception:
                    continue
            sender.send_message(f"{ColorFormat.RED}Couldn't figure out which world you're in.")
            return (None, None)

        # Hub + no name: error
        sender.send_message(f"{ColorFormat.RED}Usage from the hub: /world like <owner>")
        return (None, None)

    def _save_vote_data(self, uuid, world):
        """Save vote changes back to data file (handles both hub and world mode)."""
        if _IS_HUB:
            self.data["worlds"][uuid] = world
            self.save_data()
        else:
            data = self._load_data_world()
            data["worlds"][uuid] = world
            self._save_data_world(data)

    def _cmd_like(self, sender, owner_name):
        uuid, world = self._find_target_world_for_vote(sender, owner_name)
        if not world:
            return
        sender_uuid = str(sender.unique_id)

        if uuid == sender_uuid:
            sender.send_message(f"{ColorFormat.RED}You can't like your own world!")
            return

        likes = world.setdefault("likes", [])
        dislikes = world.setdefault("dislikes", [])

        # Toggle like
        if sender_uuid in likes:
            likes.remove(sender_uuid)
            sender.send_message(f"{ColorFormat.YELLOW}Removed your like.")
        else:
            likes.append(sender_uuid)
            # Remove dislike if previously disliked
            if sender_uuid in dislikes:
                dislikes.remove(sender_uuid)
            sender.send_message(f"{ColorFormat.GREEN}You §a§lliked§r§a {world.get('display_name', 'this world')}!")
            self._maybe_unlock_achievements(sender_uuid, "like")

        self._save_vote_data(uuid, world)

    def _cmd_dislike(self, sender, owner_name):
        uuid, world = self._find_target_world_for_vote(sender, owner_name)
        if not world:
            return
        sender_uuid = str(sender.unique_id)

        if uuid == sender_uuid:
            sender.send_message(f"{ColorFormat.RED}You can't dislike your own world!")
            return

        likes = world.setdefault("likes", [])
        dislikes = world.setdefault("dislikes", [])

        # Toggle dislike
        if sender_uuid in dislikes:
            dislikes.remove(sender_uuid)
            sender.send_message(f"{ColorFormat.YELLOW}Removed your dislike.")
        else:
            dislikes.append(sender_uuid)
            # Remove like if previously liked
            if sender_uuid in likes:
                likes.remove(sender_uuid)
            sender.send_message(f"{ColorFormat.RED}You disliked {world.get('display_name', 'this world')}.")

        self._save_vote_data(uuid, world)

    # ===== LOCATE PLAYER UI =====
    def _open_locate_form(self, sender):
        """Modal form for picking a player to locate."""
        # Build list of players currently online anywhere via presence file
        try:
            presence = self._read_presence()
            seen = set()
            online_anywhere = []
            for sid, info in presence.get("servers", {}).items():
                for p in info.get("players", []):
                    if p.lower() not in seen:
                        seen.add(p.lower())
                        online_anywhere.append(p)
            online_anywhere.sort()
        except Exception:
            online_anywhere = []

        if online_anywhere:
            form = ModalForm(
                title="§l§bLocate Player",
                controls=[
                    Label(text=f"§7Pick from §f{len(online_anywhere)}§7 players online or type a name."),
                    Dropdown(label="Online Players", options=["(type below)"] + online_anywhere, default_index=0),
                    TextInput(label="Or type a name", placeholder="Player name"),
                ],
                on_submit=lambda p, json_str: self._on_locate_submit(p, json_str, online_anywhere),
            )
        else:
            form = ModalForm(
                title="§l§bLocate Player",
                controls=[
                    Label(text="§7No players are online anywhere."),
                    TextInput(label="Player name", placeholder="Player name"),
                ],
                on_submit=lambda p, json_str: self._on_locate_submit_text_only(p, json_str),
            )
        sender.send_form(form)

    def _on_locate_submit(self, player, json_str, online_anywhere):
        try:
            v = json.loads(json_str)
            dropdown_idx = v[1]
            text_input = (v[2] or "").strip() if len(v) > 2 else ""
            if dropdown_idx > 0:
                target = online_anywhere[dropdown_idx - 1]
            elif text_input:
                target = text_input
            else:
                player.send_message(f"{ColorFormat.RED}Please pick a player or type a name.")
                return
            self._cmd_locate(player, target)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _on_locate_submit_text_only(self, player, json_str):
        try:
            v = json.loads(json_str)
            target = (v[1] or "").strip() if len(v) > 1 else ""
            if not target:
                player.send_message(f"{ColorFormat.RED}Please type a player name.")
                return
            self._cmd_locate(player, target)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_world_details(self, sender, uuid):
        """Show details for a world with Join/Like/Dislike buttons."""
        worlds = self._get_worlds_data()
        w = worlds.get(uuid)
        if not w:
            sender.send_message(f"{ColorFormat.RED}World not found.")
            return

        name = w.get("display_name", f"{w.get('owner_name', '?')}'s World")
        icon = w.get("icon", "")
        cat = w.get("category", "Other")
        owner = w.get("owner_name", "?")
        wtype = w.get("world_type", "normal")
        gm = w.get("gamemode", "survival")
        diff = w.get("difficulty", "easy")
        desc = w.get("description", "")
        likes = len(w.get("likes", []))
        dislikes = len(w.get("dislikes", []))
        online = self._get_online_count_for_world(uuid)
        sender_uuid = str(sender.unique_id)
        liked_by_me = sender_uuid in w.get("likes", [])
        disliked_by_me = sender_uuid in w.get("dislikes", [])
        is_owner = sender_uuid == uuid

        content_lines = [
            f"§a§l{icon} {name}",
            f"§7Owner: §f{owner}",
            f"§7Category: §f{cat}",
            f"§7Type: §f{wtype}, {gm}, {diff}",
            f"§7Online: §f{online}",
            f"§a▲ {likes} §7likes  §c▼ {dislikes} §7dislikes",
        ]
        if desc:
            content_lines.append("")
            content_lines.append(f"§e\"{desc}\"")
        content = "\n".join(content_lines)

        form = ActionForm(
            title=f"§l§b{name}",
            content=content,
        )
        # Join button - works differently from hub vs inside another world
        if _IS_HUB:
            form.add_button(f"§a▶ Join {owner}'s World",
                on_click=lambda p, n=owner: self._cmd_join(p, n))
        else:
            form.add_button(f"§a▶ Join {owner}'s World",
                on_click=lambda p, n=owner: self._world_browse_join(p, n))

        # Like/dislike (not for owner)
        if not is_owner:
            like_label = "§a✓ You liked this" if liked_by_me else "§a▲ Like"
            dislike_label = "§c✓ You disliked this" if disliked_by_me else "§c▼ Dislike"
            form.add_button(like_label, on_click=lambda p, u=uuid: self._vote_from_details(p, u, "like"))
            form.add_button(dislike_label, on_click=lambda p, u=uuid: self._vote_from_details(p, u, "dislike"))

        form.add_button("§7Back to Browse", on_click=lambda p: self._open_browse_menu(p))
        sender.send_form(form)

    def _vote_from_details(self, player, uuid, vote_type):
        """Handle like/dislike from the world details screen."""
        worlds = self._get_worlds_data()
        w = worlds.get(uuid)
        if not w:
            return
        owner_name = w.get("owner_name", "")
        if vote_type == "like":
            self._cmd_like(player, owner_name)
        else:
            self._cmd_dislike(player, owner_name)
        # Reopen the details so they see the updated state
        self._open_world_details(player, uuid)

    # =========================================================================
    # PHASE 4 — WORLD MODES (Play / Build / Dev)
    # =========================================================================
    def _open_settings_mode(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        modes = ["play", "build", "dev"]
        labels = ["Play (visitors can't break/place)", "Build (visitors can build)", "Dev (only owner)"]
        idx = modes.index(w.get("mode", "play")) if w.get("mode", "play") in modes else 0
        form = ModalForm(
            title="§l§3World Mode",
            controls=[
                Dropdown(label="Mode", options=labels, default_index=idx),
                Label(text="§7Play: visitors can move and chat but not modify the world.\n§7Build: visitors get build permissions.\n§7Dev: visitors can't do anything (private).\n§7Per-player permissions can override these defaults."),
            ],
            on_submit=self._on_settings_mode_submit,
        )
        sender.send_form(form)

    def _on_settings_mode_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            modes = ["play", "build", "dev"]
            w["mode"] = modes[v[0]]
            self.save_data()
            # Apply via gamemode commands if running
            if owner_uuid in self.running_worlds:
                self._push_mode_to_world(owner_uuid)
            player.send_message(f"{ColorFormat.GREEN}World mode set to §f{w['mode']}.")
            self._open_settings_menu(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _push_mode_to_world(self, owner_uuid):
        """Send commands to the world server's stdin to enforce the mode.
        For Play mode: set non-owners to adventure (can't break blocks).
        For Build mode: set non-owners to creative.
        For Dev mode: kick non-owner non-friends.
        """
        if owner_uuid not in self.running_worlds:
            return
        if owner_uuid not in self.data["worlds"]:
            return
        info = self.running_worlds[owner_uuid]
        world = self.data["worlds"][owner_uuid]
        process = info.get("process")
        if not process or not process.stdin:
            return
        mode = world.get("mode", "play")
        # We don't have a way from here to know who's in the world server,
        # so we just write a marker file the world plugin can read on tick.
        try:
            mode_file = os.path.join(world["path"], "mode.txt")
            with open(mode_file, "w") as f:
                f.write(mode)
        except Exception as e:
            self.logger.warning(f"Failed to write mode file: {e}")

    # =========================================================================
    # PHASE 6 — PLAYER PROFILES, FRIENDS, PER-WORLD PERMISSIONS
    # =========================================================================
    def _read_players(self):
        try:
            if os.path.exists(self.PLAYERS_FILE):
                with open(self.PLAYERS_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {"players": {}}

    def _write_players(self, data):
        try:
            with open(self.PLAYERS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to write players: {e}")

    def _get_player_profile(self, uuid_str, name=None):
        """Get or create a profile for a player UUID."""
        data = self._read_players()
        if uuid_str not in data["players"]:
            data["players"][uuid_str] = {
                "name": name or "?",
                "bio": "",
                "friends": [],  # list of UUIDs
                "friend_requests": [],  # incoming
                "achievements_unlocked": [],
                "stats": {
                    "worlds_visited": 0,
                    "likes_given": 0,
                    "dislikes_given": 0,
                    "first_seen": time.time(),
                },
            }
            self._write_players(data)
        elif name and data["players"][uuid_str].get("name") != name:
            data["players"][uuid_str]["name"] = name
            self._write_players(data)
        return data["players"][uuid_str]

    def _save_player_profile(self, uuid_str, profile):
        data = self._read_players()
        data["players"][uuid_str] = profile
        self._write_players(data)

    def _find_uuid_by_name(self, name):
        """Look up a player UUID by their name. Checks worlds data first, then players file."""
        name_lower = name.lower()
        # Check world owners
        worlds = self._get_worlds_data()
        for uuid, w in worlds.items():
            if w.get("owner_name", "").lower() == name_lower:
                return uuid
        # Check players file
        pdata = self._read_players()
        for uuid, p in pdata.get("players", {}).items():
            if p.get("name", "").lower() == name_lower:
                return uuid
        # Check online players
        try:
            for p in self.server.online_players:
                if p.name.lower() == name_lower:
                    return str(p.unique_id)
        except Exception:
            pass
        return None

    def _open_profile_menu(self, sender, target_name):
        """Show a player's profile (their own or someone else's)."""
        target_uuid = self._find_uuid_by_name(target_name)
        if not target_uuid:
            sender.send_message(f"{ColorFormat.RED}Player '{target_name}' not found.")
            return
        profile = self._get_player_profile(target_uuid, name=target_name)

        # Find their worlds
        worlds = self._get_worlds_data()
        owned = [w for uuid, w in worlds.items() if uuid == target_uuid]

        # Where are they currently?
        sid, where = self._find_player_world(profile.get("name", target_name))
        location_str = f"§a{where}" if where else "§7offline"

        is_self = str(sender.unique_id) == target_uuid

        bio = profile.get("bio", "") or "§7(no bio)"
        stats = profile.get("stats", {})
        achievements = profile.get("achievements_unlocked", [])

        content_lines = [
            f"§l§b{profile.get('name', '?')}",
            f"§7Status: {location_str}",
            f"§7Worlds owned: §f{len(owned)}",
            f"§7Worlds visited: §f{stats.get('worlds_visited', 0)}",
            f"§7Likes given: §a{stats.get('likes_given', 0)}",
            f"§7Achievements: §e{len(achievements)}",
            "",
            f"§e{bio}",
        ]
        content = "\n".join(content_lines)

        form = ActionForm(
            title="§l§bProfile",
            content=content,
        )
        if is_self:
            form.add_button("§aEdit Bio", on_click=lambda p: self._open_edit_bio(p))
        else:
            sender_profile = self._get_player_profile(str(sender.unique_id), name=sender.name)
            already_friend = target_uuid in sender_profile.get("friends", [])
            already_requested = str(sender.unique_id) in profile.get("friend_requests", [])
            if already_friend:
                form.add_button("§c✗ Remove Friend", on_click=lambda p, t=target_uuid: self._remove_friend(p, t))
            elif already_requested:
                form.add_button("§7✓ Request Sent", on_click=lambda p: self._open_profile_menu(p, target_name))
            else:
                form.add_button("§a+ Add Friend", on_click=lambda p, t=target_uuid, n=target_name: self._send_friend_request(p, t, n))

            if where and where != "the hub":
                form.add_button(f"§b▶ Join {profile.get('name', '?')} on {where}",
                    on_click=lambda p, n=profile.get('name', '?'): self._cmd_locate(p, n))

        if owned:
            for w in owned[:5]:
                wname = w.get("display_name", "World")
                form.add_button(f"§7View: {wname}", on_click=lambda p, ow=w.get("owner_name"): self._cmd_join(p, ow) if _IS_HUB else None)

        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p) if _IS_HUB else self._open_main_menu_world(p))
        sender.send_form(form)

    def _open_edit_bio(self, sender):
        profile = self._get_player_profile(str(sender.unique_id), name=sender.name)
        form = ModalForm(
            title="§l§aEdit Bio",
            controls=[
                TextInput(label="Your bio (max 200 chars)", default_value=profile.get("bio", "")),
            ],
            on_submit=self._on_edit_bio_submit,
        )
        sender.send_form(form)

    def _on_edit_bio_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            uuid = str(player.unique_id)
            profile = self._get_player_profile(uuid, name=player.name)
            profile["bio"] = (v[0] or "").strip()[:200]
            self._save_player_profile(uuid, profile)
            player.send_message(f"{ColorFormat.GREEN}Bio updated!")
            self._open_profile_menu(player, player.name)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _send_friend_request(self, sender, target_uuid, target_name):
        sender_uuid = str(sender.unique_id)
        if target_uuid == sender_uuid:
            sender.send_message(f"{ColorFormat.RED}You can't friend yourself!")
            return
        target = self._get_player_profile(target_uuid, name=target_name)
        if sender_uuid not in target.get("friend_requests", []):
            target.setdefault("friend_requests", []).append(sender_uuid)
            self._save_player_profile(target_uuid, target)
        sender.send_message(f"{ColorFormat.GREEN}Friend request sent to {target_name}!")
        # Cross-server notify
        self._send_cross_server_message(target_name,
            f"§a{sender.name} sent you a friend request! Use /world friends to respond.")

    def _remove_friend(self, sender, friend_uuid):
        sender_uuid = str(sender.unique_id)
        sp = self._get_player_profile(sender_uuid, name=sender.name)
        fp = self._get_player_profile(friend_uuid)
        if friend_uuid in sp.get("friends", []):
            sp["friends"].remove(friend_uuid)
            self._save_player_profile(sender_uuid, sp)
        if sender_uuid in fp.get("friends", []):
            fp["friends"].remove(sender_uuid)
            self._save_player_profile(friend_uuid, fp)
        sender.send_message(f"{ColorFormat.YELLOW}Removed {fp.get('name', '?')} from your friends.")
        self._open_friends_menu(sender)

    def _open_friends_menu(self, sender):
        sender_uuid = str(sender.unique_id)
        profile = self._get_player_profile(sender_uuid, name=sender.name)
        friends = profile.get("friends", [])
        requests = profile.get("friend_requests", [])

        content = f"§a§lFriends: §f{len(friends)}\n§eIncoming requests: §f{len(requests)}"
        form = ActionForm(
            title="§l§aFriends",
            content=content,
        )
        # Friend requests
        for req_uuid in requests[:10]:
            req_profile = self._get_player_profile(req_uuid)
            req_name = req_profile.get("name", "?")
            form.add_button(f"§e+ Accept {req_name}", on_click=lambda p, r=req_uuid: self._accept_friend(p, r))
        # Friends
        for friend_uuid in friends[:20]:
            fp = self._get_player_profile(friend_uuid)
            name = fp.get("name", "?")
            sid, where = self._find_player_world(name)
            status = f"§a● {where}" if where else "§7○ offline"
            form.add_button(f"§b{name}\n{status}", on_click=lambda p, n=name: self._open_profile_menu(p, n))
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p) if _IS_HUB else self._open_main_menu_world(p))
        sender.send_form(form)

    def _accept_friend(self, sender, requester_uuid):
        sender_uuid = str(sender.unique_id)
        sp = self._get_player_profile(sender_uuid, name=sender.name)
        rp = self._get_player_profile(requester_uuid)
        if requester_uuid in sp.get("friend_requests", []):
            sp["friend_requests"].remove(requester_uuid)
        if requester_uuid not in sp.get("friends", []):
            sp.setdefault("friends", []).append(requester_uuid)
        if sender_uuid not in rp.get("friends", []):
            rp.setdefault("friends", []).append(sender_uuid)
        self._save_player_profile(sender_uuid, sp)
        self._save_player_profile(requester_uuid, rp)
        sender.send_message(f"{ColorFormat.GREEN}You and {rp.get('name', '?')} are now friends!")
        self._maybe_unlock_achievements(sender_uuid, "make_friend")
        self._maybe_unlock_achievements(requester_uuid, "make_friend")
        self._send_cross_server_message(rp.get("name", ""),
            f"§a{sender.name} accepted your friend request!")
        self._open_friends_menu(sender)

    # ===== PER-PLAYER WORLD PERMISSIONS =====
    def _open_settings_player_perms(self, sender):
        owner_uuid = str(sender.unique_id)
        if owner_uuid not in self.data["worlds"]:
            return
        w = self.data["worlds"][owner_uuid]
        perms = w.get("player_perms", {})
        form = ActionForm(
            title="§l§2Player Permissions",
            content="§7Override world mode for specific players.",
        )
        form.add_button("§a+ Add/Edit Player Permission", on_click=lambda p: self._open_add_player_perm(p))
        for uuid, perm in list(perms.items())[:20]:
            # Look up name from profile
            profile = self._get_player_profile(uuid)
            name = profile.get("name", uuid[:8])
            build = "✓" if perm.get("build", False) else "✗"
            form.add_button(f"§f{name}\n§7Build: {build}", on_click=lambda p, u=uuid, n=name: self._open_edit_player_perm(p, u, n))
        form.add_button("§7Back", on_click=lambda p: self._open_settings_menu(p))
        sender.send_form(form)

    def _open_add_player_perm(self, sender):
        form = ModalForm(
            title="§l§2Add Player Permission",
            controls=[
                TextInput(label="Player Name", placeholder="ExactName"),
                Toggle(label="Allow Build", default_value=True),
                Toggle(label="Allow Commands", default_value=False),
            ],
            on_submit=self._on_add_player_perm_submit,
        )
        sender.send_form(form)

    def _on_add_player_perm_submit(self, player, json_str):
        try:
            v = json.loads(json_str)
            owner_uuid = str(player.unique_id)
            w = self.data["worlds"].get(owner_uuid)
            if not w:
                return
            target_name = (v[0] or "").strip()
            if not target_name:
                player.send_message(f"{ColorFormat.RED}Need a player name.")
                return
            target_uuid = self._find_uuid_by_name(target_name)
            if not target_uuid:
                player.send_message(f"{ColorFormat.RED}Player '{target_name}' not found.")
                return
            w.setdefault("player_perms", {})[target_uuid] = {
                "build": bool(v[1]),
                "use_commands": bool(v[2]),
            }
            self.save_data()
            player.send_message(f"{ColorFormat.GREEN}Permissions set for {target_name}.")
            self._open_settings_player_perms(player)
        except Exception as e:
            player.send_message(f"{ColorFormat.RED}Failed: {e}")

    def _open_edit_player_perm(self, sender, target_uuid, target_name):
        owner_uuid = str(sender.unique_id)
        w = self.data["worlds"].get(owner_uuid)
        if not w:
            return
        perm = w.get("player_perms", {}).get(target_uuid, {})
        form = ActionForm(
            title=f"§l§2{target_name}",
            content=f"§7Build: §f{perm.get('build', False)}\n§7Commands: §f{perm.get('use_commands', False)}",
        )
        form.add_button("§eToggle Build", on_click=lambda p, u=target_uuid: self._toggle_perm(p, u, "build"))
        form.add_button("§eToggle Commands", on_click=lambda p, u=target_uuid: self._toggle_perm(p, u, "use_commands"))
        form.add_button("§cRemove All Permissions", on_click=lambda p, u=target_uuid: self._clear_perm(p, u))
        form.add_button("§7Back", on_click=lambda p: self._open_settings_player_perms(p))
        sender.send_form(form)

    def _toggle_perm(self, player, target_uuid, key):
        owner_uuid = str(player.unique_id)
        w = self.data["worlds"].get(owner_uuid)
        if not w:
            return
        perm = w.setdefault("player_perms", {}).setdefault(target_uuid, {})
        perm[key] = not perm.get(key, False)
        self.save_data()
        profile = self._get_player_profile(target_uuid)
        self._open_edit_player_perm(player, target_uuid, profile.get("name", "?"))

    def _clear_perm(self, player, target_uuid):
        owner_uuid = str(player.unique_id)
        w = self.data["worlds"].get(owner_uuid)
        if not w:
            return
        if target_uuid in w.get("player_perms", {}):
            del w["player_perms"][target_uuid]
            self.save_data()
        player.send_message(f"{ColorFormat.YELLOW}Permissions cleared.")
        self._open_settings_player_perms(player)

    # =========================================================================
    # PHASE 7 — TEMPLATES, ACHIEVEMENTS, LEADERBOARDS
    # =========================================================================
    # Achievement definitions
    ACHIEVEMENTS = {
        "first_world": {"name": "World Builder", "desc": "Create your first world", "icon": "🏗️"},
        "ten_worlds_created": {"name": "Architect", "desc": "Create 10 worlds (lifetime)", "icon": "🏛️"},
        "first_visit": {"name": "Tourist", "desc": "Visit another player's world", "icon": "🧳"},
        "ten_visits": {"name": "Explorer", "desc": "Visit 10 worlds", "icon": "🗺️"},
        "fifty_visits": {"name": "Globetrotter", "desc": "Visit 50 worlds", "icon": "🌎"},
        "first_like": {"name": "Critic", "desc": "Like or dislike your first world", "icon": "👍"},
        "first_friend": {"name": "Social Butterfly", "desc": "Make your first friend", "icon": "🦋"},
        "ten_friends": {"name": "Popular", "desc": "Have 10 friends", "icon": "🌟"},
        "ten_likes_received": {"name": "Crowd Pleaser", "desc": "Receive 10 likes on a world", "icon": "💯"},
        "fifty_likes_received": {"name": "Famous", "desc": "Receive 50 likes on a world", "icon": "⭐"},
        "owned_world_visited": {"name": "Host", "desc": "Have someone visit your world", "icon": "🏠"},
    }

    def _maybe_unlock_achievements(self, player_uuid, event_type, **kwargs):
        """Check if any achievements should unlock based on the event."""
        try:
            profile = self._get_player_profile(player_uuid)
            unlocked = set(profile.get("achievements_unlocked", []))
            new_unlocks = []

            stats = profile.setdefault("stats", {})

            if event_type == "create_world":
                stats["worlds_created"] = stats.get("worlds_created", 0) + 1
                if stats["worlds_created"] >= 1 and "first_world" not in unlocked:
                    new_unlocks.append("first_world")
                if stats["worlds_created"] >= 10 and "ten_worlds_created" not in unlocked:
                    new_unlocks.append("ten_worlds_created")
            elif event_type == "visit_world":
                stats["worlds_visited"] = stats.get("worlds_visited", 0) + 1
                if stats["worlds_visited"] >= 1 and "first_visit" not in unlocked:
                    new_unlocks.append("first_visit")
                if stats["worlds_visited"] >= 10 and "ten_visits" not in unlocked:
                    new_unlocks.append("ten_visits")
                if stats["worlds_visited"] >= 50 and "fifty_visits" not in unlocked:
                    new_unlocks.append("fifty_visits")
            elif event_type == "like":
                stats["likes_given"] = stats.get("likes_given", 0) + 1
                if "first_like" not in unlocked:
                    new_unlocks.append("first_like")
            elif event_type == "make_friend":
                friend_count = len(profile.get("friends", []))
                if friend_count >= 1 and "first_friend" not in unlocked:
                    new_unlocks.append("first_friend")
                if friend_count >= 10 and "ten_friends" not in unlocked:
                    new_unlocks.append("ten_friends")

            for ach_id in new_unlocks:
                unlocked.add(ach_id)
                ach = self.ACHIEVEMENTS[ach_id]
                # Notify player
                try:
                    name = profile.get("name", "")
                    self._send_cross_server_message(name,
                        f"§6§l⭐ Achievement Unlocked! §r§e{ach['icon']} {ach['name']}\n§7{ach['desc']}")
                except Exception:
                    pass

            profile["achievements_unlocked"] = list(unlocked)
            self._save_player_profile(player_uuid, profile)
        except Exception as e:
            self.logger.warning(f"Achievement check error: {e}")

    def _open_achievements_menu(self, sender):
        sender_uuid = str(sender.unique_id)
        profile = self._get_player_profile(sender_uuid, name=sender.name)
        unlocked = set(profile.get("achievements_unlocked", []))
        total = len(self.ACHIEVEMENTS)
        got = len(unlocked & set(self.ACHIEVEMENTS.keys()))
        content = f"§a§lUnlocked: §f{got}/{total}"
        form = ActionForm(
            title="§l§6Achievements",
            content=content,
        )
        for ach_id, ach in self.ACHIEVEMENTS.items():
            status = "§a✓" if ach_id in unlocked else "§7✗"
            label = f"{status} §f{ach['icon']} {ach['name']}\n§7{ach['desc']}"
            form.add_button(label, on_click=lambda p: None)
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p) if _IS_HUB else self._open_main_menu_world(p))
        sender.send_form(form)

    # ===== LEADERBOARDS =====
    def _open_leaderboard_menu(self, sender):
        form = ActionForm(
            title="§l§5Leaderboards",
            content="§7See who's on top!",
        )
        form.add_button("§a▲ Most Liked Worlds", on_click=lambda p: self._show_leaderboard(p, "most_liked_worlds"))
        form.add_button("§eMost Visited Worlds", on_click=lambda p: self._show_leaderboard(p, "most_visited_worlds"))
        form.add_button("§bMost Achievements", on_click=lambda p: self._show_leaderboard(p, "most_achievements"))
        form.add_button("§dMost Friends", on_click=lambda p: self._show_leaderboard(p, "most_friends"))
        form.add_button("§6Most Worlds Owned", on_click=lambda p: self._show_leaderboard(p, "most_worlds_owned"))
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p) if _IS_HUB else self._open_main_menu_world(p))
        sender.send_form(form)

    def _show_leaderboard(self, sender, kind):
        worlds = self._get_worlds_data()
        pdata = self._read_players()
        players = pdata.get("players", {})

        title = ""
        lines = []

        if kind == "most_liked_worlds":
            title = "§l§a▲ Most Liked Worlds"
            ranked = sorted(worlds.items(), key=lambda kv: -len(kv[1].get("likes", [])))
            for i, (uuid, w) in enumerate(ranked[:10], 1):
                lines.append(f"§7#{i} §f{w.get('display_name', 'World')} §7by §f{w.get('owner_name', '?')} §a▲{len(w.get('likes', []))}")
        elif kind == "most_visited_worlds":
            title = "§l§eMost Visited Worlds"
            ranked = sorted(worlds.items(), key=lambda kv: -kv[1].get("play_count", 0))
            for i, (uuid, w) in enumerate(ranked[:10], 1):
                lines.append(f"§7#{i} §f{w.get('display_name', 'World')} §7by §f{w.get('owner_name', '?')} §e{w.get('play_count', 0)} visits")
        elif kind == "most_achievements":
            title = "§l§bMost Achievements"
            ranked = sorted(players.items(), key=lambda kv: -len(kv[1].get("achievements_unlocked", [])))
            for i, (uuid, p) in enumerate(ranked[:10], 1):
                lines.append(f"§7#{i} §f{p.get('name', '?')} §b⭐{len(p.get('achievements_unlocked', []))}")
        elif kind == "most_friends":
            title = "§l§dMost Friends"
            ranked = sorted(players.items(), key=lambda kv: -len(kv[1].get("friends", [])))
            for i, (uuid, p) in enumerate(ranked[:10], 1):
                lines.append(f"§7#{i} §f{p.get('name', '?')} §d{len(p.get('friends', []))} friends")
        elif kind == "most_worlds_owned":
            title = "§l§6Most Worlds Owned"
            counts = {}
            for uuid, w in worlds.items():
                owner = w.get("owner_name", "?")
                counts[owner] = counts.get(owner, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            for i, (name, cnt) in enumerate(ranked[:10], 1):
                lines.append(f"§7#{i} §f{name} §6{cnt} worlds")

        content = "\n".join(lines) if lines else "§7No data yet."
        form = ActionForm(
            title=title,
            content=content,
        )
        form.add_button("§7Back", on_click=lambda p: self._open_leaderboard_menu(p))
        sender.send_form(form)

    # ===== TEMPLATES (Clone a world's settings) =====
    def _open_templates_menu(self, sender):
        """Show worlds you can clone the settings from."""
        worlds = self._get_worlds_data()
        sender_uuid = str(sender.unique_id)

        # Available templates: any public world OR worlds you own
        templates = []
        for uuid, w in worlds.items():
            if uuid == sender_uuid:
                continue  # not your own
            if w.get("public", False) or sender_uuid in w.get("allowed_players", []):
                templates.append((uuid, w))

        if not templates:
            sender.send_message(f"{ColorFormat.YELLOW}No public worlds to use as templates yet.")
            return

        form = ActionForm(
            title="§l§3World Templates",
            content="§7Pick a world to copy settings from. (Doesn't copy actual blocks - just settings!)",
        )
        for uuid, w in templates[:20]:
            owner = w.get("owner_name", "?")
            cat = w.get("category", "Other")
            form.add_button(f"§f{w.get('display_name', 'World')}\n§7by {owner} | {cat}",
                on_click=lambda p, n=owner: self._cmd_clone(p, n))
        form.add_button("§7Back", on_click=lambda p: self._open_main_menu_hub(p))
        sender.send_form(form)

    def _cmd_clone(self, sender, owner_name):
        """Clone settings from another player's world to create yours."""
        sender_uuid = str(sender.unique_id)
        if sender_uuid in self.data["worlds"]:
            sender.send_message(f"{ColorFormat.RED}You already have a world! Delete it first.")
            return

        # Find source world
        source = None
        for uuid, w in self.data["worlds"].items():
            if w.get("owner_name", "").lower() == owner_name.lower():
                source = w
                break
        if not source:
            sender.send_message(f"{ColorFormat.RED}No world found for {owner_name}.")
            return

        # Check we have access
        if not source.get("public", False) and sender_uuid not in source.get("allowed_players", []):
            sender.send_message(f"{ColorFormat.RED}That world is private.")
            return

        port = self.get_next_available_port()
        if port is None:
            sender.send_message(f"{ColorFormat.RED}No available ports.")
            return

        world_dir = os.path.join(self.WORLDS_BASE_DIR, sender_uuid)
        if hasattr(self, "_pending_deletes"):
            self._pending_deletes.discard(world_dir)
        try:
            os.makedirs(world_dir, exist_ok=True)
            self._write_world_properties(world_dir, port, sender.name,
                source.get("world_type", "normal"),
                source.get("gamemode", "creative"),
                world_data=source)
            self._write_ops_file(world_dir, sender.name, str(sender.xuid))
            Thread(target=self._copy_plugins_to_world, args=(world_dir,), daemon=True).start()
        except Exception as e:
            sender.send_message(f"{ColorFormat.RED}Failed: {e}")
            return

        # Create the new world data, copying most settings from source but keeping ownership
        import copy
        new_world = copy.deepcopy(source)
        new_world.update({
            "owner_name": sender.name,
            "port": port,
            "path": world_dir,
            "allowed_players": [sender_uuid],
            "name_invites": [],
            "display_name": f"{sender.name}'s World",
            "created_at": time.time(),
            "last_played_at": time.time(),
            "play_count": 0,
            "likes": [],
            "dislikes": [],
            "achievements_unlocked": [],
            "player_perms": {},
        })
        self.data["worlds"][sender_uuid] = new_world
        self.save_data()

        self._maybe_unlock_achievements(sender_uuid, "create_world")
        sender.send_message(f"{ColorFormat.GREEN}Cloned {owner_name}'s world settings!")
        sender.send_message(f"{ColorFormat.GREEN}Use {ColorFormat.YELLOW}/world join {sender.name}{ColorFormat.GREEN} to enter.")
