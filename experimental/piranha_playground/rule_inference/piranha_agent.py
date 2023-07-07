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

import copy
import difflib
import logging
import multiprocessing
import re
from typing import List, Optional, Tuple

import attr
import toml
from piranha_playground.rule_inference.controller import Controller
from piranha_playground.rule_inference.graph_parser import TemplateParser
from piranha_playground.rule_inference.piranha_chat import (
    PiranhaChatException,
    PiranhaGPTChat,
)
from piranha_playground.rule_inference.rule_application import run_piranha_with_timeout
from piranha_playground.rule_inference.static_inference import Inference, QueryWriter
from piranha_playground.rule_inference.utils.logger_formatter import CustomFormatter
from piranha_playground.rule_inference.utils.node_utils import NodeUtils
from piranha_playground.rule_inference.utils.pretty_toml import PrettyTOML
from piranha_playground.rule_inference.utils.rule_utils import RawRuleGraph
from polyglot_piranha import (
    PiranhaArguments,
    PiranhaOutputSummary,
    Rule,
    RuleGraph,
    execute_piranha,
)
from tree_sitter import Tree
from tree_sitter_languages import get_language, get_parser

logger = logging.getLogger("PiranhaChat")
logger.setLevel(logging.DEBUG)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(CustomFormatter())
logger.addHandler(ch)


class PiranhaAgentError(Exception):
    pass


@attr.s
class PiranhaAgent:
    """
    An agent that uses OpenAI's chat models for inferring Piranha rules.
    The agent takes pairs of source and target codes, finds a transformation rule between them,
    and validates the rule's effectiveness by testing if the rule can refactor the source code into the target code.

    :ivar source_code: The source code to refactor
    :ivar target_code: The target code we want to achieve after refactoring
    :ivar language: The programming language of the source code (default is "java")
    :ivar hints: Any hints or information that might help in inferring the rules
    :ivar chat: Holds the chat completion and followups
    :ivar tree_sitter_language: The language object for parsing code with tree-sitter
    :ivar tree_sitter_parser: The parser object for parsing code with tree-sitter
    :ivar explanation: Explanation of the rule generated by GPT
    :ivar rules: Holds the inferred rules in TOML format
    """

    source_code = attr.ib(type=str)
    target_code = attr.ib(type=str)
    language = attr.ib(default="java")
    hints = attr.ib(default="")
    chat = attr.ib(default=None)
    tree_sitter_language = attr.ib(default=None)
    tree_sitter_parser = attr.ib(default=None)
    explanation = attr.ib(default=None)
    rules = attr.ib(default=None)
    language_mappings = {
        "java": "java",
        "kt": "kotlin",
    }  # This is necessary because get_parser and piranha expect different naming conventions

    def __attrs_post_init__(self):
        """
        Initialize parser and language attributes for the given language after the agent object is created.
        """
        self.tree_sitter_language = get_language(
            self.language_mappings.get(self.language, self.language)
        )
        self.parser = get_parser(
            self.language_mappings.get(self.language, self.language)
        )

    def get_tree_from_code(self, code: str) -> Tree:
        """
        Parse the given code and return its abstract syntax tree (AST).

        :param code: The source code to parse
        :type code: str
        :return: AST of the source code
        :rtype: Tree"""
        tree = self.parser.parse(bytes(code, "utf8"))
        return tree

    def infer_rules_statically(self) -> str:
        """This function creates the first pass of the rule inference process.
        It statically infers rules from the example code and returns a TOML representation of the rule graph.

        :return: str, string containing the rule in TOML format
        """
        source_tree = self.get_tree_from_code(self.source_code)
        target_tree = self.get_tree_from_code(self.target_code)

        rules = {}
        finder = TemplateParser(source_tree, target_tree)
        pairs = finder.parse_templates()
        for comment_name, (nodes_before, nodes_after) in pairs.items():
            inference_engine = Inference(nodes_before, nodes_after)
            rule = inference_engine.static_infer()
            rules[comment_name] = rule

        # build a dict using finder.edges but with the rule names from rule_names
        edges = {
            rules[from_name].name: [rules[to_name].name for to_name in to_names]
            for from_name, to_names in finder.edges.items()
        }
        edges = [
            {"from": k, "to": v, "scope": "File"} for k, v in edges.items() if v != []
        ]
        graph = RawRuleGraph(list(rules.values()), edges)
        self.rules = graph.to_toml()
        return self.rules

    def infer_rules(self) -> str:
        """
        Interacts with the AI model to refine statically generated rules.

        :return: The rule inferred from GPT
        :rtype: str

        """
        try:
            chat_interactions = self.create_chats(self.rules)
        except BaseException as e:
            raise PiranhaAgentError(str(e)) from e

        # For each completion try to transform the source code into the target code
        return self.iterate_inference(chat_interactions)

    def remove_comments_from_code(self, code: str) -> str:
        """
        Removes all comments from the given code using Piranha.

        :param code: The source code from which to remove comments
        :type code: str
        :return: Source code without comments
        :rtype: str
        """
        rule = Rule(
            name="remove_comments",
            query="(line_comment) @comment",
            replace_node="comment",
            replace="",
        )
        graph = RuleGraph(rules=[rule], edges=[])
        args = PiranhaArguments(
            code_snippet=code,
            language=self.language,
            rule_graph=graph,
            dry_run=True,
        )
        output_summaries = execute_piranha(args)
        if output_summaries:
            return output_summaries[0].content
        return code

    def create_chats(self, rules) -> List[PiranhaGPTChat]:
        """
        Prepare the data for interaction with the AI model.

        :param rules: Statically inferred rules in TOML format
        :type rules: str
        :return: List of chat interactions with information necessary for AI model.
        :rtype: List[PiranhaGPTChat]
        """

        self.source_code = self.remove_comments_from_code(self.source_code)
        self.target_code = self.remove_comments_from_code(self.target_code)

        source_tree = self.get_tree_from_code(self.source_code)
        target_tree = self.get_tree_from_code(self.target_code)

        source_tree_sexpr = NodeUtils.generate_sexpr(source_tree.root_node, 0)
        target_tree_sexpr = NodeUtils.generate_sexpr(target_tree.root_node, 0)
        # Create diff between source and target code using difflib
        diff = list(
            difflib.unified_diff(
                self.source_code.splitlines(), self.target_code.splitlines()
            )
        )
        diff = "\n".join(diff)
        # diff = self.append_diff_information(diff, source_tree, target_tree)
        # Cleanup source
        prompt_holes = {
            "source_code": self.source_code,
            "source_tree": source_tree_sexpr,
            "target_tree": target_tree_sexpr,
            "diff": diff,
            "rules": rules,
            "hints": self.hints,
        }
        # Number of Chat interactions to have with the model
        n_samples = 15
        chat_interactions = [
            PiranhaGPTChat(holes=prompt_holes) for _ in range(n_samples)
        ]
        try:
            first_round = chat_interactions[0].get_completion(n_samples=n_samples)
        except PiranhaChatException as e:
            logger.debug(
                f"Chat completion failed with {e}. Trying again with a new chat...\n"
            )
            return []
        for i, response in enumerate(first_round):
            # Hack to prevent running the prompt multiple times (it's the same for all samples)
            # It is cheaper just to sample OpenAI API
            chat_interactions[i].append_system_message(response)
        return chat_interactions

    def get_explanation(self):
        return self.explanation

    def iterate_inference(self, chat_interactions: List[PiranhaGPTChat]) -> str:
        """
        Find a rule generated by chat models that complies with the user specified example.

        :param chat_interactions: List of chat sessions for the inference engine to use
        :type chat_interactions: List[PiranhaGPTChat]
        :return: The rule inferred from GPT
        :rtype: str
        :raises PiranhaAgentError: If the agent fails to generate a rule after 10 rounds of interaction with GPT-4.
        """
        max_rounds = 10
        for i in range(max_rounds):
            for chat in chat_interactions:
                try:
                    completion = chat.get_model_response()
                    _, toml_block, explanation = self.validate_rule(completion)
                    self.chat = chat
                    self.explanation = explanation
                    return toml_block
                except PiranhaAgentError as e:
                    logger.debug(
                        f"GPT-4 failed to generate a rule. Following up the next round with {e}. Trying again...\n"
                    )
                    chat.append_user_followup(str(e))
                except PiranhaChatException as e:
                    logger.debug(
                        f"Chat completion failed with {e}. Trying again with a new chat...\n"
                    )
        raise PiranhaAgentError(
            f"Failed to generate a rule after {max_rounds} rounds of interaction with GPT-4.",
        )

    def validate_rule(self, completion) -> Tuple[str, str, str]:
        """
        Tests if the inferred rule can transform the source code into the target code.

        :param completion: Inferred rule from the model
        :type completion: str
        :return: A tuple containing the file name, TOML block, and the explanation
        :rtype: Tuple[str, str, str]
        """
        pattern = r"```toml(?!md)(.*?)```"
        logger.debug(f"Completion\n: {completion}")
        # Extract all toml block contents
        toml_blocks = re.findall(pattern, completion, re.DOTALL)
        if not toml_blocks:
            raise PiranhaAgentError(
                "No TOML block provided in the expected output format. "
                "Please provide a TOML block with the rule. ```toml ... ```"
            )

        pattern = r"```md(.*?)```"
        explanation = re.findall(pattern, completion, re.DOTALL)

        if not explanation:
            raise PiranhaAgentError(
                "No explanation provided in the expected output format. "
                "Please provide an explanation as a markdown block. ```md ... ```"
            )

        try:
            toml_block = (
                toml_blocks[0].replace("parenthesized_expression", "condition").strip()
            )
            logger.debug(f"Generated rule: {toml_block}")
            toml_dict = toml.loads(toml_block)
        except Exception as e:
            raise PiranhaAgentError(
                f"Could not create Piranha rule. The TOML block is not valid: {e}. "
            )

        refactored_code = self.run_piranha(toml_dict)
        if not refactored_code:
            raise PiranhaAgentError(
                "Piranha did not generate any refactored code. Either the query or the filters are incorrect. "
            )
        if NodeUtils.normalize_code(refactored_code) != NodeUtils.normalize_code(
            self.target_code
        ):
            raise PiranhaAgentError(
                f"The rule produced wrong code!!! "
                f"Expected:\n{self.target_code}\n\n but got:\n{refactored_code}\n\n"
            )
        pattern = r"<file_name_start>(.*?)<file_name_end>"
        file_names = re.findall(pattern, completion, re.DOTALL)
        file_name = file_names[0] if file_names else "rule.toml"
        return file_name, toml_block, explanation[0]

    def run_piranha(self, toml_dict) -> str:
        """
        Runs the inferred rule graph by applying it to the source code using Piranha.

        :param toml_dict: Inferred rules in TOML format
        :type toml_dict: dict
        :return: Refactored code as a result of the rule application
        :rtype: str
        """
        rules = toml_dict.get("rules", [])
        if not rules:
            raise PiranhaAgentError("TOML does not include any rule specifications.")
        try:
            raw_graph = RawRuleGraph.from_toml(toml_dict)
            logger.debug(f"Raw graph: {raw_graph.to_toml()}")

            res, success = run_piranha_with_timeout(
                self.source_code, self.language, raw_graph, timeout=5
            )

            if not success:
                if "QueryError" in res:
                    raise PiranhaAgentError(
                        f"One of the provided queries is not valid {res}. "
                        f"Do not use nodes you cannot see in the tree representation. "
                        f"Make sure you parenthesis are balanced."
                    )
                raise PiranhaAgentError(f"Piranha failed to execute: {res}.")
            return res

        except multiprocessing.context.TimeoutError:
            raise PiranhaAgentError(
                "Piranha in infinite loop. Please add a filter or constraint the query. "
                "Remember you can only constraint queries with #eq, #not-eq, #match. "
                "Otherwise you need to use a [[rules.filters]] with contains or not_contains."
            )

    def improve_rule(self, task: str, rules: str) -> str:
        """
        Improves the rule by adding a filter to it.

        :param task: str: Description of what you would like to do
        :param rules: str: Rules to improve
        :return: rule: str: The improved rule
        :raises PiranhaAgentError: If unable to improve the rule
        """
        max_rounds = 15
        if self.chat is None:
            logger.debug("Unable to improve rule because chat is None.")
            raise PiranhaAgentError(
                "Improvement is only support for GPT inferred rules."
            )

        chat = copy.deepcopy(self.chat)
        for _ in range(max_rounds):
            try:
                controller = Controller(chat)
                updated_rules = []
                explanations = []
                for rule in rules.get("rules", []):
                    rule_str = toml.dumps(rule, encoder=PrettyTOML())
                    should_improve = controller.should_improve_rule(task, rule_str)
                    if should_improve:
                        option = controller.get_option_for_improvement(rule_str)
                        if option == "add filter":
                            rule, explanation = self.add_filter(task, rule, chat)
                            updated_rules.append(rule)
                            explanations.append(explanation)
                            continue
                    updated_rules.append(rule)
                rule_block = "\n".join(
                    [toml.dumps(rule, encoder=PrettyTOML()) for rule in updated_rules]
                )
                explanation_block = "\n".join(explanations)
                validation = self.validate_rule(
                    f"<file_name_start>rules.toml<file_name_end> ```toml\n{rule_block}\n``` ```md\n{explanation_block}\n```"
                )

                self.chat = chat
                self.explanation = "\n".join(explanations)
                return True, validation[1]
            except Exception as e:
                logger.debug(
                    f"GPT-4 failed to generate a rule. Following up the next round with {e}. Trying again...\n"
                )
                chat.append_user_followup(str(e))

        logger.debug(f"Unable to improve rule {rules}.")
        raise PiranhaAgentError("Unable to improve rule.")

    def add_filter(self, desc, rule, chat) -> Tuple[dict, str]:
        """
        Adds a filter to the rule that encloses the nodes of the rule.

        :param desc: Description of what you would like to do
        :type desc: str
        :param rule: Rule to add a filter to
        :type rule: dict
        :param chat: Chat interactions with information necessary for AI model
        :type chat: PiranhaGPTChat
        :return: A tuple containing the rule with the added filter and its explanation
        :rtype: Tuple[dict, str]
        """

        query = rule.get("query")
        source_tree = self.get_tree_from_code(self.source_code)
        tree_sitter_q = self.tree_sitter_language.query(query)
        captures = tree_sitter_q.captures(source_tree.root_node)
        captures = NodeUtils.get_smallest_nonoverlapping_set([c[0] for c in captures])

        parents = []
        for node in captures:
            while node:
                parents.append(node)
                node = node.parent

        enclosing_nodes = parents
        enclosing_options = ""

        for i, node in enumerate(enclosing_nodes):
            qw = QueryWriter([node])
            query = qw.write(simplify=True)
            enclosing_options += f"\n\n=== Option {i} ===\n\n"
            enclosing_options += f'enclosing_node = """{query}"""\n'

        # Get the nodes that can be used as enclosing node for the rules
        chat.append_improve_request(
            desc,
            toml.dumps(rule, encoder=PrettyTOML()),
            enclosing_options,
        )
        completion = chat.get_model_response()
        pattern = r"```toml(?!md)(.*?)```"
        # Extract all toml block contents
        toml_blocks = re.findall(pattern, completion, re.DOTALL)

        pattern = r"```md(.*?)```"
        explanation = re.findall(pattern, completion, re.DOTALL)

        return toml.loads(toml_blocks[0]), explanation[0]
