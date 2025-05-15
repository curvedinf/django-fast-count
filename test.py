#!/usr/bin/env python
import os
import sys
import subprocess


def main():
    # Determine paths
    # The script itself is in the project root.
    project_root = os.path.dirname(os.path.abspath(__file__))
    # The Django test project is located at tests/djangotest
    django_test_project_dir = os.path.join(project_root, "tests", "djangotest")
    # The source code of the package being tested is in src/
    src_dir = os.path.join(project_root, "src")

    # Prepare environment variables
    env = os.environ.copy()
    # Set DJANGO_SETTINGS_MODULE for the test Django project
    # This refers to tests/djangotest/djangotest/settings.py
    env["DJANGO_SETTINGS_MODULE"] = "djangotest.settings"

    # Adjust PYTHONPATH:
    # 1. Add `django_test_project_dir` so 'import djangotest.settings' works.
    #    (djangotest/settings.py relative to django_test_project_dir)
    # 2. Add `src_dir` so 'import django_fast_count' works, finding the code in src/.
    #    This is important if 'pip install -e .' hasn't been run or isn't fully effective.
    python_path_parts = [
        django_test_project_dir,  # For 'import djangotest.settings'
        src_dir,  # For 'import django_fast_count'
    ]
    if "PYTHONPATH" in env:
        # Append existing PYTHONPATH if it's set
        python_path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_parts)

    # Prepare pytest command
    # Add coverage reporting
    coverage_report_dir = os.path.join(project_root, "htmlcov")
    # Base command for pytest with coverage
    command_base = [
        "pytest",
        "--cov=django_fast_count",  # Measure coverage for the django_fast_count package (in src/)
        f"--cov-report=html:{coverage_report_dir}",  # Generate HTML report in project_root/htmlcov
        "--cov-report=term-missing",  # Print a summary of missing lines to the terminal
    ]
    # Append any arguments received by this script to pytest
    command = command_base + sys.argv[1:]

    # Print information for clarity during execution
    print(f"Project Root: {project_root}")
    print(f"Django Test Project Directory (CWD for pytest): {django_test_project_dir}")
    print(f"Src Directory (for django_fast_count): {src_dir}")
    print(f"DJANGO_SETTINGS_MODULE: {env['DJANGO_SETTINGS_MODULE']}")
    print(f"PYTHONPATH: {env['PYTHONPATH']}")
    print(f"Coverage Report (HTML): {os.path.join(coverage_report_dir, 'index.html')}")
    print(f"Executing: \"{' '.join(command)}\"")

    # Execute pytest from the Django test project directory
    # This is often recommended by pytest-django, as it helps find manage.py
    process = subprocess.Popen(command, cwd=django_test_project_dir, env=env)
    process.wait()  # Wait for the pytest process to complete

    # Exit with the same return code as pytest
    sys.exit(process.returncode)


if __name__ == "__main__":
    main()
