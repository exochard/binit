# shellcheck shell=bash
workspace_directory() {
    local project_type=''
    if [[ -f Cargo.toml ]]; then project_type=Rust
    elif [[ -f pyproject.toml || -f requirements.txt ]]; then project_type=Python
    elif [[ -f package.json ]]; then project_type=Node
    elif [[ -f go.mod ]]; then project_type=Go
    fi
    binit_segment workspace "$project_type"
}
binit_on directory workspace_directory
