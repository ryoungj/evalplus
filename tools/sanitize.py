"""Purpose of this file: Sanitize the code produced by LLMs for the following reasons.
1. Vicuna generated code could miss one white space. We fix the white space to make Vicuna more capable.
2. {Our fault lol.} We find more EOFs tokens afterwards and truncate some messy code afterwards.
"""

import os
import ast
import re

from tqdm import tqdm

from evalplus.data import (
    get_human_eval_plus,
    get_mbpp_plus,
    load_solutions,
    write_directory,
    write_jsonl,
)
from evalplus.sanitize import sanitize


def remove_unindented_lines(code, protect_before, execeptions, trim_tails):
    lines = code.splitlines()
    cut_idx = []
    cut_enabled = False
    for i, line in enumerate(lines):
        if not cut_enabled and line.startswith(protect_before):
            cut_enabled = True
            continue
        if line.strip() == "":
            continue
        if any(line.startswith(e) for e in execeptions):
            continue

        lspace = len(line) - len(line.lstrip())
        if lspace == 0:
            cut_idx.append(i)

        if any(line.rstrip().startswith(t) for t in trim_tails):
            # cut off everything behind
            cut_idx.extend(list(range(i, len(lines))))
            break

    return "\n".join([line for i, line in enumerate(lines) if i not in cut_idx])


def to_four_space_indents(old_code):
    new_code = ""
    for line in old_code.splitlines():
        lspace = len(line) - len(line.lstrip())
        if lspace == 3:
            new_code += " "
        new_code += line + "\n"
    return new_code


"""From https://github.com/evalplus/evalplus/issues/84"""
def coding_humaneval_match_answer(task_data, response):
    # Matching utilities
    def _function_exists(code, func_name):
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return True

        return False

    def _try_match(content, prefix, entrypoint):
        # All markdown code blocks, as well as raw
        code_blocks = [m[1] for m in re.findall(r"(\`{3}.*?\n+)([\s\S]*?)(\n+\`{3})", content)] \
                    + [content]

        for block in code_blocks:
            # Check syntax
            try:
                code_completion = prefix + block
                if _function_exists(code_completion, entrypoint):
                    return code_completion
            except SyntaxError:
                pass

    # Try match with include prefix
    humaneval_task = task_data
    include_prefix = humaneval_task['prompt'].split('def')[0].strip() + "\n\n"

    result = _try_match(response, include_prefix, humaneval_task["entry_point"])
    if result: 
        return True, {"task_id": humaneval_task["task_id"], "completion": result}

    # If fail then match with function signature
    result = _try_match(response, humaneval_task["prompt"], humaneval_task["entry_point"])
    if result: 
        return True, {"task_id": humaneval_task["task_id"], "completion": result}

    return False, {"task_id": humaneval_task["task_id"], "completion": response}


if __name__ == "__main__":
    import argparse
    import pathlib

    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=str, required=True)
    parser.add_argument("--eofs", nargs="+", type=str, default=[])
    parser.add_argument("--clean-chat-response", action="store_true")
    parser.add_argument("--inplace", action="store_true")
    parser.add_argument(
        "--rm-prefix-lines", type=str, help="Remove lines starting with this", nargs="+"
    )
    parser.add_argument(
        "--dataset", required=True, type=str, choices=["humaneval", "mbpp"]
    )
    parser.add_argument(
        "--debug-task", type=str, help="Enter the task ID to only sanitize that task."
    )
    args = parser.parse_args()

    # task_id -> entry_point
    entry_point = {}

    if args.dataset == "humaneval":
        dataset = get_human_eval_plus()
    elif args.dataset == "mbpp":
        dataset = get_mbpp_plus()

    for task_id, problem in dataset.items():
        entry_point[task_id] = problem["entry_point"]

    # make a new folder with "-sanitized" suffix
    is_folder = os.path.isdir(args.samples)
    target_path = pathlib.Path(args.samples)
    if not args.inplace:
        if is_folder:
            new_name = target_path.name + "-sanitized"
        else:
            new_name = target_path.name.replace(".jsonl", "-sanitized.jsonl")
        target_path = target_path.parent / new_name
    target_path = str(target_path)

    nsan = 0
    ntotal = 0
    nmatched = 0

    new_solutions = []

    for solution in tqdm(load_solutions(args.samples)):
        task_id = solution["task_id"]
        dbg_identifier = solution["_identifier"]
        if args.debug_task is not None and task_id != args.debug_task:
            continue

        ntotal += 1
        if "solution" in solution:
            old_code = solution["solution"]
        else:
            assert "completion" in solution
            old_code = dataset[task_id]["prompt"] + "\n" + solution["completion"]

        old_code = old_code.strip()

        if args.clean_chat_response:
            macthed, cleaned_output = coding_humaneval_match_answer(dataset[task_id], old_code)
            _new_code = cleaned_output["completion"]
            if macthed:
                nmatched += 1
        else:
            _new_code = old_code

        new_code = sanitize(
            old_code=_new_code,
            entry_point=entry_point[task_id],
            rm_prefix_lines=args.rm_prefix_lines,
            eofs=args.eofs,
        ).strip()

        # if changed, print the message
        if new_code != old_code:
            msg = "Sanitized: " + dbg_identifier
            if is_folder:
                msg += " -> " + dbg_identifier.replace(args.samples, target_path)
            print(msg)
            nsan += 1

        new_solutions.append({"task_id": task_id, "solution": new_code})

    if is_folder:
        write_directory(target_path, new_solutions)
        write_jsonl(os.path.join(target_path, "samples-sanitized.jsonl"), new_solutions)
    else:
        write_jsonl(target_path, new_solutions)

    if args.clean_chat_response:
        print(f"Matched {nmatched} out of {ntotal} files.")

    print(f"Sanitized {nsan} out of {ntotal} files.")
