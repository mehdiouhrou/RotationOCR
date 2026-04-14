from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_store import sanitize_relative_path, validate_pdf_file


def test_sanitize_relative_path_accepts_nested_path():
    assert sanitize_relative_path("RH/2024/contrat1.pdf") == "RH/2024/contrat1.pdf"


def test_sanitize_relative_path_blocks_traversal():
    try:
        sanitize_relative_path("../secret.pdf")
        assert False, "Expected ValueError for path traversal"
    except ValueError:
        assert True


def test_validate_pdf_file():
    assert validate_pdf_file("file.pdf") is True
    assert validate_pdf_file("file.txt") is False


if __name__ == "__main__":
    test_sanitize_relative_path_accepts_nested_path()
    test_sanitize_relative_path_blocks_traversal()
    test_validate_pdf_file()
    print("tests_ok")
