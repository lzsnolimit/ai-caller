"""Run the server Live worker with independent phone-model configuration.

Read the API key from the existing environment. Do not save or print it.
The API provider can use Codex configuration (optional); the phone model does not.
"""

import os
from pathlib import Path
import sys
import tomllib
from urllib.parse import urlsplit, urlunsplit


def main():
    repo = Path(__file__).resolve().parent
    runner = repo / "node_modules/.bin/tsx"
    if not runner.is_file():
        raise SystemExit("Run npm ci in server/common/ai first.")
    args = sys.argv[1:]
    if "--status" in args:
        os.chdir(repo)
        os.execve(str(runner), ["tsx", "src/index.ts", *args], os.environ.copy())
    codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    # Codex configuration is optional; without it, use OpenAI and OPENAI_API_KEY.
    try:
        with (codex_dir / "config.toml").open("rb") as source:
            config = tomllib.load(source)
    except FileNotFoundError:
        config = {}
    provider_name = config.get("model_provider", "openai")
    provider = config.get("model_providers", {}).get(provider_name, {})
    key_name = provider.get("env_key", "OPENAI_API_KEY")
    key = os.environ.get(key_name)
    if not key:
        raise SystemExit(f"Required environment variable is not set: {key_name}")
    base = urlsplit(provider.get("base_url", "https://api.openai.com/v1"))
    if base.scheme != "https" or base.username or base.password or base.query or base.fragment:
        raise SystemExit("The Codex API base URL must be a plain HTTPS URL.")
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = key
    env["OPENAI_LIVE_URL"] = urlunsplit(
        ("wss", base.netloc, base.path.rstrip("/") + "/live/sessions", "", "")
    )
    env["OPENAI_BACKEND_MODEL"] = os.environ.get("OPENAI_BACKEND_MODEL", "").strip() or "gpt-6-luna"
    if provider_name == "azure":
        env["OPENAI_LIVE_AUTH_HEADER"] = "api-key"
    else:
        env.pop("OPENAI_LIVE_AUTH_HEADER", None)
    os.chdir(repo)
    if env.get("PHONE_AUDIO_FD"):
        node = env.get("PHONE_NODE") or "node"
        os.execvpe(node, [node, "--import", str(repo / "node_modules/tsx/dist/loader.mjs"), str(repo / "src/index.ts"), *args], env)
    os.execve(str(runner), ["tsx", "src/index.ts", *args], env)


if __name__ == "__main__":
    main()
