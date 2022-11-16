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

package main

import "fmt"

func a() bool {
	enabled := exp.BoolValue("true")

	return enabled
}

func b() string {
	enabled := exp.BoolValue("true")

	s, err := exp.StrValue("str")
	if err != nil {
		fmt.Println(err)
	}

	if enabled {
		return s
	} else {
		return "prefix_" + s
	}
}

func c() string {
	enabled := exp.BoolValue("false")

	s, err := exp.StrValue("str")
	if err != nil {
		fmt.Println(err)
	}

	if enabled {
		return s
	} else {
		fmt.Println("not enabled")
		return "prefix_" + s
	}
}

func after_return1() string {
	enabled := exp.BoolValue("false")
	if !enabled {
		return "not enabled"
	}

	return "enabled"
}

func after_return2(a bool) string {
	if a {
		enabled := exp.BoolValue("false")
		if !enabled {
			fmt.Println("not enabled")
			return "not enabled"
		}
		fmt.Println("should be removed")
	}
	// delete after return needs to consider blocks
	fmt.Println("should not be removed")
	return "keep"
}
