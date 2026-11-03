"""Compile native bridges outside the repository. No Bluetooth operations."""
import os
from pathlib import Path
import subprocess
import sys

source = Path(__file__).resolve().parent
output = Path(os.environ.get('PHONE_MAC_NATIVE_DIR', str(Path.home() / '.cache/phone-call-gateway/mac-native')))
if sys.platform != 'darwin':
    raise SystemExit('This bridge requires macOS.')
output.mkdir(parents=True, exist_ok=True)
for filename, library, frameworks in (
    ('acipc_bridge.c', 'libacipc_bridge.dylib', ['CoreFoundation', 'IOKit']),
):
    args = ['clang', '-dynamiclib', '-Wall', '-Wextra', '-Werror', str(source / filename), '-o', str(output / library)]
    for framework in frameworks:
        args.extend(['-framework', framework])
    subprocess.run(args, check=True)
print('Mac native bridges compiled.')
