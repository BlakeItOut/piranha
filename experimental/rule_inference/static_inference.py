# Copyright (c) 2023 Uber Technologies, Inc.
#
# <p>Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of the License at
# <p>http://www.apache.org/licenses/LICENSE-2.0
#
# <p>Unless required by applicable law or agreed to in writing, software distributed under the
# License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing permissions and
# limitations under the License.

from typing import List

import attr
from comby import Comby
from experimental.rule_inference.utils.node_utils import NodeUtils
from experimental.rule_inference.utils.rule_utils import RawRule
from tree_sitter import Node, TreeCursor


@attr.s
class QueryWriter:
    """
    Class to represent a query writer for nodes.

    :ivar seq_nodes: Sequence of nodes for which the query will be written.
    :ivar capture_groups: A dictionary to keep track of the nodes that will be captured in the query.
    :ivar count: A counter for naming the capture groups.
    :ivar query_str: The Comby pattern that represents the query.
    :ivar query_ctrs: List of constraints for the Comby pattern.
    :ivar outer_most_node: Represents the node that is currently the furthest from the root.
    """

    seq_nodes = attr.ib(type=list)
    capture_groups = attr.ib(factory=dict)
    count = attr.ib(default=0)
    query_str = attr.ib(default="")
    query_ctrs = attr.ib(factory=list)
    outer_most_node = attr.ib(default=None)

    def write(self, simplify=False):
        """
        Get textual representation of the sequence.
        Find for each named child of source_node, can we replace it with its respective target group.

        :param simplify: If True, simplify the query.
        :return: The query string
        """

        if self.query_str:
            return self.query_str

        node_queries = [
            self.write_query(node, simplify=simplify) for node in self.seq_nodes
        ]

        self.query_str = ".".join(node_queries) + "\n" + "\n".join(self.query_ctrs)
        self.query_str = f"({self.query_str})"

        return self.query_str

    def write_query(self, node: Node, depth=0, prefix="", simplify=False):
        """
        Write a query for a given node, considering its depth and prefix.

        :param node: The node for which the query will be written.
        :param depth: The current depth of the node.
        :param prefix: Prefix for the current node.
        :param simplify: If True, simplify the query.
        :return: The query string for this node.
        """

        indent = " " * depth
        cursor: TreeCursor = node.walk()
        s_exp = indent + f"{prefix}({node.type} "

        next_child = cursor.goto_first_child()
        while next_child:
            child_node: Node = cursor.node
            if child_node.is_named:
                s_exp += "\n"
                prefix = (
                    f"{cursor.current_field_name()}: "
                    if cursor.current_field_name()
                    else ""
                )
                if simplify:
                    s_exp += (
                        " " * (depth + 1) + f"({child_node.type}) @tag{self.count}n"
                    )
                    self.count += 1
                    if child_node.child_count == 0:
                        self.query_ctrs.append(
                            f"(#eq? @tag{self.count}n \"{child_node.text.decode('utf8')}\")"
                        )
                else:
                    s_exp += self.write_query(cursor.node, depth + 1, prefix, simplify)
            next_child = cursor.goto_next_sibling()

        self.count += 1
        node_name = f"@tag{self.count}n"
        self.capture_groups[node_name] = node

        # if the node is an identifier, add it to eq constraints
        if node.child_count == 0:
            self.query_ctrs.append(f"(#eq? {node_name} \"{node.text.decode('utf8')}\")")

        self.outer_most_node = node_name
        return s_exp + f") {node_name}"

    def simplify_query(self, capture_group):
        """
        Simplify a query removing all the children of capture_group and replacing it with a wildcard node.
        This should be replaced with Piranha at some point.

        :param capture_group: The capture group to simplify.
        """

        comby = Comby()
        match = f"(:[[node_name]] :[_]) {capture_group}"
        rewrite = f"(:[[node_name]]) {capture_group}"
        self.query_str = comby.rewrite(self.query_str, match, rewrite)

        # Now for every child of capture_group, we need to remove equality checks from the query
        stack = [self.capture_groups[capture_group]]
        while stack:
            first = stack.pop()
            to_remove = next(
                key for key, value in self.capture_groups.items() if value == first
            )
            match = f"(#eq? {to_remove} :[_])"
            self.query_str = comby.rewrite(self.query_str, match, "")
            self.capture_groups.pop(to_remove, None)
            for child in first.named_children:
                stack.append(child)

    def replace_with_tags(self, replace_str: str) -> str:
        """
        Replace nodes with their corresponding capture group in the replace_str.

        :param replace_str: The string that needs replacement.
        :return: The replaced string.
        """
        for capture_group, node in sorted(
            self.capture_groups.items(), key=lambda x: -len(x[1].text)
        ):
            if capture_group not in self.capture_groups.keys():
                continue
            text_repr = NodeUtils.convert_to_source(node)
            if text_repr in replace_str:
                # self.simplify_query(capture_group)
                replace_str = replace_str.replace(text_repr, f"{capture_group}")
        return replace_str


@attr.s
class Inference:
    """
    Class to represent inference on nodes.

    :ivar nodes_before: The list of nodes before the transformation.
    :ivar nodes_after: The list of nodes after the transformation.
    :ivar name: Name of the inference rule.
    :ivar _counter: A class-wide counter for naming the inference rules."""

    nodes_before = attr.ib(type=List[Node], validator=attr.validators.instance_of(list))
    nodes_after = attr.ib(type=List[Node], validator=attr.validators.instance_of(list))
    name = attr.ib(
        init=False,
    )

    _counter = 0

    def __attrs_post_init__(self):
        """
        Initialization method to increment the counter and set the name for the rule.
        """
        type(self)._counter += 1
        self.name = f"rule_{type(self)._counter}"

    def static_infer(self) -> RawRule:
        """
        Infer a raw rule based on the nodes before and after.

        :return: A raw rule inferred from the nodes."""
        if len(self.nodes_after) > 0 and len(self.nodes_before) > 0:
            return self.create_rule(self.nodes_before, self.nodes_after)
        elif len(self.nodes_after) > 0:
            raise self.create_addition()
        elif len(self.nodes_before) > 0:
            return self.create_rule(self.nodes_before, [])

    def find_nodes_to_change(self, node_before: Node, node_after: Node):
        """
        Function to find nodes to change if there's only one diverging node.

        :param node_before: The node before the change.
        :param node_after: The node after the change.
        :return: The nodes that need to be changed.
        """
        diverging_nodes = []
        if node_before.type == node_after.type:
            # Check if there's only one and only one diverging node
            # If there's more than one, then we can't do anything
            diverging_nodes = [
                (child_before, child_after)
                for child_before, child_after in zip(
                    node_before.named_children, node_after.named_children
                )
                if NodeUtils.convert_to_source(child_before)
                != NodeUtils.convert_to_source(child_after)
            ]

        if (
            len(diverging_nodes) == 1
            and node_before.child_count == node_after.child_count
        ):
            return self.find_nodes_to_change(*diverging_nodes[0])

        return node_before, node_after

    def create_rule(self, nodes_before: List[Node], nodes_after: List[Node]) -> RawRule:
        """
        Create a rule based on the nodes before and after.

        :param nodes_before: The list of nodes before the change.
        :param nodes_after: The list of nodes after the change.
        :return: A raw rule representing the transformation from nodes_before to nodes_after.
        """
        if len(nodes_before) == 1:
            if len(nodes_after) == 1:
                nodes_before[0], nodes_after[0] = self.find_nodes_to_change(
                    nodes_before[0], nodes_after[0]
                )
            node = nodes_before[0]
            qw = QueryWriter([node])
            query = qw.write()

            lines_affected = " ".join(
                [NodeUtils.convert_to_source(node) for node in nodes_after]
            )
            replacement_str = qw.replace_with_tags(lines_affected)

            return RawRule(
                name=self.name,
                query=query,
                replace_node=qw.outer_most_node[1:],
                replace=replacement_str,
            )

        # If there are multiple nodes
        else:
            ancestor = NodeUtils.find_lowest_common_ancestor(nodes_before)
            replacement_str = NodeUtils.convert_to_source(
                ancestor, exclude=nodes_before
            )
            replacement_str = replacement_str.replace(
                "{placeholder}", "", len(nodes_before) - 1
            )

            lines_affected = " ".join(
                [NodeUtils.convert_to_source(node) for node in nodes_after]
            )
            replacement_str = replacement_str.replace(
                "{placeholder}", lines_affected, 1
            )
            qw = QueryWriter([ancestor])
            qw.write()
            replacement_str = qw.replace_with_tags(replacement_str)

            return RawRule(
                name=self.name,
                query=qw.query_str,
                replace_node=qw.outer_most_node[1:],
                replace=replacement_str,
            )

    def create_addition(self) -> str:
        """
        A method to create addition rules. Currently not implemented.

        :raise: NotImplementedError"""
        raise NotImplementedError
