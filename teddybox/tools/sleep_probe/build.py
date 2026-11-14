"""Build the standalone probe using a copy of the application's configuration."""
from pathlib import Path
import os
import subprocess

root = Path(__file__).resolve().parents[2]
output = root / 'build/sleep-probe'
output.mkdir(parents=True, exist_ok=True)
config = (root / 'sdkconfig').read_text()
# Custom partition filenames are resolved against the standalone project.
for key in ('CONFIG_PARTITION_TABLE_CUSTOM_FILENAME', 'CONFIG_PARTITION_TABLE_FILENAME'):
    config = '\n'.join(f'{key}="{root / "part.lst"}"' if line.startswith(key + '=') else line
                       for line in config.split('\n'))
(output / 'sdkconfig').write_text(config)
idf = Path(os.environ['IDF_PATH']) / 'tools/idf.py'
subprocess.run(['python', str(idf), '-C', str(Path(__file__).resolve().parent),
                '-B', str(output), '-D', f'SDKCONFIG={output / "sdkconfig"}',
                '-D', 'IDF_TARGET=esp32s3',
                'build'], check=True)
