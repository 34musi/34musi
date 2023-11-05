import pytest

from common.yaml_util import clean_extract_yaml


@pytest.fixture(scope="session", autouse=True)
def clear_extract():
    clean_extract_yaml()
