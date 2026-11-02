#!/usr/bin/env python3
"""Start the installed loopback component from an interactive user session."""
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen

try:
    request = Request('http://127.0.0.1:18743/health', headers={'Origin': 'https://liangzai4322.github.io'})
    with urlopen(request, timeout=2) as response:
        if response.status == 200:
            print('无痕同步加速组件已在运行。')
            raise SystemExit(0)
except OSError:
    pass
app = Path('/Volumes/Samsung SSD/Applications/simple-netdisk-accelerator')
config = Path.home()/'Library/Application Support/simple-netdisk-accelerator'
with (config/'runtime.log').open('ab') as log:
    subprocess.Popen(['/usr/bin/python3', str(app/'native_accelerator.py')], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, close_fds=True, start_new_session=True)
print('加速组件已启动，请回到无痕同步页面点击“连接加速”。')
