"""Pinned regression fixture only, not a production source selector or a Google write."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import Mock
from okx_v09_reporting.model import Model
from okx_v09_reporting.common import read_json
from okx_v09_reporting.artifact import verify_artifact
from okx_v09_drive_backup.github_source import GitHubSource,Selection

if __name__=='__main__':
    model=Model(os.environ['FROZEN_MODEL_ROOT'])
    artifact=verify_artifact(Path(os.environ['PHASE7_SOURCE_ARCHIVE']).read_bytes(),read_json(Path(os.environ['PHASE7_SOURCE_CONTEXT']).read_bytes()),model)
    source=Mock(spec=GitHubSource);source.read_latest.return_value=(Selection(artifact.context,'2026-10-29T16:00:00Z',{}),artifact)
    spec=importlib.util.spec_from_file_location('producer',Path(__file__).with_name('apps_script_export.py'));producer=importlib.util.module_from_spec(spec);spec.loader.exec_module(producer)
    producer.build(source,model,'offline-fixture',os.environ['APPS_SCRIPT_FIXTURE'])
