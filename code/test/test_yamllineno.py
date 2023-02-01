from io import StringIO
from healthstatus.yamllineno import load_yaml_lineno


def test_load_yaml_lineno() -> None:
    fp = StringIO(
        "key1:\n"
        "  key1_1: item1\n"
        "  key1_2: item1_2\n"
        "  key1_3:\n"
        "    - item1_3_1\n"
        "    - item1_3_2\n"
        "key2: item 2\n"
        "key3: another item 1\n"
    )
    assert load_yaml_lineno(fp) == {
        "key1": {
            "key1_1": "item1",
            "key1_1_lineno": 2,
            "key1_2": "item1_2",
            "key1_2_lineno": 3,
            "key1_3": ["item1_3_1", "item1_3_2"],
            "key1_3_lineno": 4,
        },
        "key1_lineno": 1,
        "key2": "item 2",
        "key2_lineno": 7,
        "key3": "another item 1",
        "key3_lineno": 8,
    }
