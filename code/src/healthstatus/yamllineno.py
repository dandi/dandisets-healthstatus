# Based on <https://stackoverflow.com/a/70679131/744178>
from typing import IO, Any
from yaml.composer import Composer
from yaml.constructor import Constructor
from yaml.loader import Loader
from yaml.nodes import ScalarNode
from yaml.resolver import BaseResolver


class LineLoader(Loader):
    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        # The line number where the previous token ended (plus empty lines):
        line = self.line
        node = Composer.compose_node(self, parent, index)
        node.__line__ = line + 1
        return node

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        node_pair_lst = node.value
        node_pair_lst_for_appending = []
        for key_node, _ in node_pair_lst:
            shadow_key_node = ScalarNode(
                tag=BaseResolver.DEFAULT_SCALAR_TAG, value=key_node.value + "_lineno"
            )
            shadow_value_node = ScalarNode(
                tag=BaseResolver.DEFAULT_SCALAR_TAG, value=key_node.__line__
            )
            node_pair_lst_for_appending.append((shadow_key_node, shadow_value_node))
        node.value = node_pair_lst + node_pair_lst_for_appending
        mapping = Constructor.construct_mapping(self, node, deep=deep)
        return mapping


def load_yaml_lineno(fp: IO[str]) -> Any:
    return LineLoader(fp).get_single_data()
