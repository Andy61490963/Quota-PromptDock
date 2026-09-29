"""發布門檻應在錯誤標籤或版本宣告時中止。"""
import pytest

from tools.check_release_version import app_version, verify_release_tag


def test_release_tag_must_match_embedded_version(tmp_path):
    source = tmp_path / "app.py"
    source.write_text('APP_VERSION = "1.4.4"\n', encoding="utf-8")
    assert app_version(source) == "1.4.4"
    verify_release_tag("v1.4.4", source)
    with pytest.raises(ValueError, match="不符"):
        verify_release_tag("v1.4.5", source)


@pytest.mark.parametrize("declaration", [
    'APP_VERSION = "1.4.4"\nAPP_VERSION = "1.4.5"\n',
    'APP_VERSION = get_version()\n',
    'VERSION = "1.4.4"\n',
])
def test_release_version_requires_one_literal_assignment(tmp_path, declaration):
    source = tmp_path / "app.py"
    source.write_text(declaration, encoding="utf-8")
    with pytest.raises(ValueError, match="APP_VERSION"):
        app_version(source)
