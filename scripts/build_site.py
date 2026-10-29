"""Render the download page from the same version as the native packages."""
import tomllib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def build(destination=None):
    version=tomllib.loads((ROOT/'pyproject.toml').read_text('utf-8'))['project']['version']
    target=Path(destination) if destination else ROOT/'output'/'site'
    target.mkdir(parents=True,exist_ok=True)
    html=(ROOT/'site'/'index.html').read_text('utf-8').replace('{{VERSION}}',version)
    (target/'index.html').write_text(html,encoding='utf-8')
    (target/'.nojekyll').touch()
    return target


if __name__=='__main__':print(build())
