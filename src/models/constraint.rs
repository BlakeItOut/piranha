/*
Copyright (c) 2022 Uber Technologies, Inc.

 <p>Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
 except in compliance with the License. You may obtain a copy of the License at
 <p>http://www.apache.org/licenses/LICENSE-2.0

 <p>Unless required by applicable law or agreed to in writing, software distributed under the
 License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
 express or implied. See the License for the specific language governing permissions and
 limitations under the License.
*/

use std::collections::HashMap;

use getset::Getters;
use serde_derive::Deserialize;
use tree_sitter::Node;

use crate::utilities::tree_sitter_utilities::{
  get_node_for_range, substitute_tags, PiranhaHelpers,
};

use super::{rule::InstantiatedRule, rule_store::RuleStore, source_code_unit::SourceCodeUnit};

#[derive(Deserialize, Debug, Clone, Hash, PartialEq, Eq, Getters)]
pub(crate) struct Constraint {
  /// Scope in which the constraint query has to be applied
  #[get = "pub"]
  matcher: String,
  /// The Tree-sitter queries that need to be applied in the `matcher` scope
  #[get = "pub"]
  #[serde(default)]
  queries: Vec<String>,
}

impl Constraint {
  #[cfg(test)]
  pub(crate) fn new(matcher: String, queries: Vec<String>) -> Self {
    Self { matcher, queries }
  }
}

// Implements instance methods related to applying a constraint
impl SourceCodeUnit {
  pub(crate) fn is_satisfied(
    &self, node: Node, rule: &InstantiatedRule, substitutions: &HashMap<String, String>,
    rule_store: &mut RuleStore,
  ) -> bool {
    let mut updated_substitutions = rule_store.piranha_args().input_substitutions().clone();
    updated_substitutions.extend(substitutions.clone());
    rule.constraints().iter().all(|constraint| {
      self._is_satisfied(constraint.clone(), node, rule_store, &updated_substitutions)
    })
  }

  /// Checks if the node satisfies the constraints.
  /// Constraint has two parts (i) `constraint.matcher` (ii) `constraint.query`.
  /// This function traverses the ancestors of the given `node` until `constraint.matcher` matches
  /// i.e. finds scope for constraint.
  /// Within this scope it checks if the `constraint.query` DOES NOT MATCH any sub-tree.
  fn _is_satisfied(
    &self, constraint: Constraint, node: Node, rule_store: &mut RuleStore,
    substitutions: &HashMap<String, String>,
  ) -> bool {
    let mut current_node = node;
    // This ensures that the below while loop considers the current node too when checking for constraints.
    // It does not make sense to check for constraint if current node is a "leaf" node.
    if node.child_count() > 0 {
      current_node = node.child(0).unwrap();
    }
    // Get the scope_node of the constraint (`scope.matcher`)
    let mut matched_matcher = false;
    while let Some(parent) = current_node.parent() {
      let matcher_query_str = substitute_tags(constraint.matcher(), substitutions, true);
      if let Some(p_match) =
        parent.get_match_for_query(self.code(), rule_store.query(&matcher_query_str), false)
      {
        matched_matcher = true;
        let scope_node = get_node_for_range(
          self.root_node(),
          p_match.range().start_byte,
          p_match.range().end_byte,
        );
        for query_with_holes in constraint.queries() {
          let query_str = substitute_tags(query_with_holes, substitutions, true);
          let query = &rule_store.query(&query_str);
          // If this query matches anywhere within the scope, return false.
          if scope_node
            .get_match_for_query(self.code(), query, true)
            .is_some()
          {
            return false;
          }
        }
        break;
      }
      current_node = parent;
    }
    matched_matcher
  }
}
