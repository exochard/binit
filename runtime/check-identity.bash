if [[ -n ${1-} ]]; then
    builtin enable -f "$1" binit_complete || exit 125
fi
builtin printf 'BINIT-IDENTITY\t%s\n' "$BASH_VERSION"
while IFS= read -r __binit_identity_mapping; do
    builtin printf '%s\n' "$__binit_identity_mapping"
done < "/proc/$$/maps"
