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

//! Defines the entry-point for Piranha.
use std::{fs, time::Instant};

use log::info;
use models::{piranha_arguments::PiranhaArguments, piranha_output::PiranhaOutputSummary};
use polyglot_piranha::execute_piranha;

fn main() {
  let now = Instant::now();
  env_logger::init();

  let args = PiranhaArguments::from_command_line();

  let piranha_output_summaries = execute_piranha(&args, true);

  if let Some(path) = args.path_to_output_summaries() {
    write_output_summary(piranha_output_summaries, path);
  }

  info!("Time elapsed - {:?}", now.elapsed().as_secs());
}

/// Writes the output summaries to a Json file named `path_to_output_summaries` .
fn write_output_summary(
  piranha_output_summaries: Vec<PiranhaOutputSummary>, path_to_json: &String,
) {
  if let Ok(contents) = serde_json::to_string_pretty(&piranha_output_summaries) {
    if fs::write(path_to_json, contents).is_ok() {
      return;
    }
  }
  panic!(
    "Could not write the output summary to the file - {}",
    path_to_json
  );
}
