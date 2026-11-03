"""Private service data. Task snapshots are committed before dialing."""
import json
import os
from pathlib import Path
import secrets

from .contracts import GatewayError
from .voices import validate_voice


def data_directory():
    return Path(os.environ.get("PHONE_DATA_DIR", str(Path.home() / ".local/share/phone-call-gateway"))).expanduser()


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_directory()

    def save(self, category, key, value):
        directory = self.root / category
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        destination = directory / (key + ".json")
        temporary = directory / ("." + secrets.token_hex(16))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, category, key, default=None):
        path = self.root / category / (key + ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def task(self, body, system_prompt, voice="marin"):
        voice = validate_voice(voice)
        if not isinstance(body, dict):
            raise GatewayError("task_required")
        purpose, outcome, content = (body.get(k) for k in ("purpose", "outcome", "content"))
        if not all(isinstance(v, str) and v.strip() for v in (purpose, outcome, content)):
            raise GatewayError("task_required")
        if any(len(v) > 4096 for v in (purpose, outcome)) or len(content.encode("utf-8")) > 65536:
            raise GatewayError("task_too_large", 413)
        source = body.get("source", "editor")
        if source not in ("editor", "upload"):
            raise GatewayError("invalid_task_source")
        task = {"id": secrets.token_hex(16), "direction": "outbound", "purpose": purpose, "outcome": outcome,
                "content": content, "source": source, "systemPrompt": system_prompt, "voice": voice}
        return self.commit_task(task)

    def inbound_task(self, device_prompt, system_prompt, guard_prompt, voice="marin"):
        """来电任务快照：目的和结果固定，保护 prompt 跟在全局 system prompt 之后，设备 prompt 作为内容。"""
        from .inbound import DEFAULT_INBOUND_PROMPT, INBOUND_OUTCOME, INBOUND_PURPOSE
        voice = validate_voice(voice)
        content = device_prompt if isinstance(device_prompt, str) and device_prompt.strip() else DEFAULT_INBOUND_PROMPT
        task = {"id": secrets.token_hex(16), "direction": "inbound", "purpose": INBOUND_PURPOSE,
                "outcome": INBOUND_OUTCOME, "content": content, "source": "inbound",
                "systemPrompt": system_prompt + "\n" + guard_prompt, "voice": voice}
        return self.commit_task(task)

    def commit_task(self, task):
        try:
            self.save("tasks", task["id"], task)
        except OSError:
            raise GatewayError("task_save_failed", 503) from None
        return task
