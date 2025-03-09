#!/usr/bin/env python3
import os
import sys
import ast
import platform
import subprocess
from datetime import datetime

# Attempt to import pathspec for parsing .gitignore patterns.
try:
    import pathspec
except ImportError:
    pathspec = None

def find_git_root(starting_directory):
    """
    Recurses upward from starting_directory until a .git folder is found.
    Returns the directory containing .git or None if not found.
    """
    current_dir = os.path.abspath(starting_directory)
    while True:
        if os.path.isdir(os.path.join(current_dir, ".git")):
            return current_dir
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            return None
        current_dir = parent_dir

def get_git_info(project_root):
    """
    Returns the current git commit hash and commit date for the repository at project_root.
    If git commands fail, returns (None, None).
    """
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project_root
        ).decode().strip()
        commit_date = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd"], cwd=project_root
        ).decode().strip()
        return commit_hash, commit_date
    except Exception:
        return None, None

def load_gitignore_spec(project_root):
    """
    If a .gitignore exists in the project root and the pathspec module is available,
    compile the .gitignore patterns into a spec and return it. Otherwise, return None.
    """
    gitignore_path = os.path.join(project_root, '.gitignore')
    if os.path.exists(gitignore_path) and pathspec:
        try:
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
            spec = pathspec.PathSpec.from_lines('gitwildmatch', lines)
            return spec
        except Exception as e:
            print(f"Error reading .gitignore: {e}")
            return None
    return None

def should_ignore(path, project_root, spec):
    """
    Determines whether the given file or directory path should be ignored.
    Ignores:
      - Anything in a __pycache__ folder.
      - Any file or directory whose name exactly matches .git, .gitignore, or .gitmodules.
      - Any file or directory inside common virtual environment or pip package folders (e.g. venv, .venv, env, site-packages).
      - Files matching patterns in the .gitignore spec (if provided).
    """
    norm_path = os.path.normpath(path)
    parts = norm_path.split(os.sep)
    if '__pycache__' in parts:
        return True
    ignore_set = {'.git', '.gitignore', '.gitmodules', 'venv', '.venv', 'env', 'site-packages'}
    for part in parts:
        if part in ignore_set:
            return True
    if spec:
        try:
            rel = os.path.relpath(path, project_root)
        except ValueError:
            rel = path
        if spec.match_file(rel):
            return True
    return False

def resolve_module(module_name, base_dir):
    """
    Given a module name (e.g. 'foo.bar'), attempt to resolve it to a file path relative to base_dir.
    Returns the file path if found, else None.
    """
    parts = module_name.split('.')
    file_path = os.path.join(base_dir, *parts) + ".py"
    if os.path.isfile(file_path):
        return os.path.abspath(file_path)
    package_path = os.path.join(base_dir, *parts, "__init__.py")
    if os.path.isfile(package_path):
        return os.path.abspath(package_path)
    return None

def resolve_import(node, current_file, project_root):
    """
    Given an import node (ast.Import or ast.ImportFrom), resolve it to file paths (if possible)
    relative to the current file's directory first, then falling back to project_root.
    """
    results = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            module_name = alias.name
            # Try resolving relative to the current file's directory first.
            current_dir = os.path.dirname(os.path.abspath(current_file))
            resolved = resolve_module(module_name, current_dir)
            if not resolved:
                resolved = resolve_module(module_name, project_root)
            if resolved:
                results.append(resolved)
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            if node.module:
                current_dir = os.path.dirname(os.path.abspath(current_file))
                resolved = resolve_module(node.module, current_dir)
                if not resolved:
                    resolved = resolve_module(node.module, project_root)
                if resolved:
                    results.append(resolved)
        else:
            # Relative import.
            current_dir = os.path.dirname(os.path.abspath(current_file))
            for _ in range(node.level - 1):
                current_dir = os.path.dirname(current_dir)
            if node.module:
                candidate_dir = os.path.join(current_dir, *node.module.split('.'))
            else:
                candidate_dir = current_dir
            file_candidate = candidate_dir + ".py"
            if os.path.isfile(file_candidate):
                results.append(os.path.abspath(file_candidate))
            else:
                init_candidate = os.path.join(candidate_dir, "__init__.py")
                if os.path.isfile(init_candidate):
                    results.append(os.path.abspath(init_candidate))
    return results

def collect_files(file_path, project_root, spec, collected=None):
    """
    Recursively collects files starting from file_path by following import statements.
    Only files within the project_root that are not ignored are collected.
    The collected dictionary maps absolute file paths to their content.
    """
    if collected is None:
        collected = {}
    abs_path = os.path.abspath(file_path)
    if abs_path in collected:
        return collected
    if should_ignore(abs_path, project_root, spec):
        return collected
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading {abs_path}: {e}")
        return collected
    collected[abs_path] = content
    try:
        tree = ast.parse(content, filename=abs_path)
    except Exception as e:
        print(f"Error parsing {abs_path}: {e}")
        return collected
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            file_paths = resolve_import(node, abs_path, project_root)
            for fp in file_paths:
                if os.path.commonpath([fp, project_root]) == project_root:
                    collect_files(fp, project_root, spec, collected)
    return collected

def generate_context(project_root, collected, spec):
    """
    Generates a string containing additional context about the project.
    This includes a helpful prompt for ChatGPT, the project root, Python version, git commit info (if available),
    the collection timestamp, a list of collected files, and a filtered directory tree of the project.
    """
    context_lines = []
    # Helpful prompt for ChatGPT.
    context_lines.append("=" * 80)
    context_lines.append("HELPFUL PROMPT FOR CHATGPT")
    context_lines.append("=" * 80)
    context_lines.append(
        "This file is an auto-generated collection of your project's source code and context. "
        "The script started at a specified Python file and recursively followed import statements "
        "to gather all relevant local project files. It excludes files or directories that are hidden, "
        "in __pycache__, part of virtual environments (e.g. venv, .venv, env), or ignored by the project's .gitignore. "
        "The following context is provided:"
    )
    context_lines.append("  - Project root directory (determined by the .git folder if present)")
    context_lines.append("  - Python version used")
    context_lines.append("  - Current Git commit hash and commit date (if available)")
    context_lines.append("  - Timestamp of when the collection was performed")
    context_lines.append("  - A list of all collected files")
    context_lines.append("  - A filtered directory tree of the project (excluding git metadata and virtual environments)")
    context_lines.append("Use this information to fully understand the context and structure of the project when reviewing the code.\n")
    
    context_lines.append("=" * 80)
    context_lines.append("PROJECT CONTEXT INFORMATION")
    context_lines.append("=" * 80)
    context_lines.append(f"Project Root: {project_root}")
    context_lines.append(f"Python Version: {platform.python_version()}")
    
    commit_hash, commit_date = get_git_info(project_root)
    if commit_hash:
        context_lines.append(f"Git Commit: {commit_hash}")
        context_lines.append(f"Commit Date: {commit_date}")
    
    context_lines.append(f"Collection Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    context_lines.append(f"Total Collected Files: {len(collected)}")
    context_lines.append("")
    context_lines.append("List of Collected Files:")
    for file in sorted(collected.keys()):
        context_lines.append(f"- {file}")
    context_lines.append("")
    context_lines.append("Filtered Project Directory Structure:")
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if not should_ignore(os.path.join(root, d), project_root, spec)]
        files = [f for f in files if not should_ignore(os.path.join(root, f), project_root, spec)]
        level = os.path.relpath(root, project_root).count(os.sep)
        indent = ' ' * 4 * level
        context_lines.append(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            context_lines.append(f"{subindent}{f}")
    context_lines.append("\n")
    return "\n".join(context_lines)

def main():
    if len(sys.argv) != 2:
        print("Usage: python collect_project.py <path/to/main.py>")
        sys.exit(1)
    starting_file = sys.argv[1]
    if not os.path.isfile(starting_file):
        print(f"Error: {starting_file} does not exist or is not a file.")
        sys.exit(1)

    starting_dir = os.path.dirname(os.path.abspath(starting_file))
    git_root = find_git_root(starting_dir)
    project_root = git_root if git_root else starting_dir
    project_name = os.path.basename(project_root)
    
    # Load .gitignore spec if available.
    spec = load_gitignore_spec(project_root)
    if not spec and pathspec is None:
        print("pathspec module not found. Install it via 'pip install pathspec' to honor .gitignore patterns.")

    collected = collect_files(starting_file, project_root, spec)
    output_file = f"{project_name}_collected_code.txt"
    with open(output_file, "w", encoding="utf-8") as out:
        context = generate_context(project_root, collected, spec)
        out.write(context)
        out.write("\n\n")
        for file, content in collected.items():
            out.write("=" * 80 + "\n")
            out.write(f"File: {file}\n")
            out.write("=" * 80 + "\n\n")
            out.write(content)
            out.write("\n\n")
    print(f"Collected {len(collected)} file(s) into {output_file}")

if __name__ == "__main__":
    main()
