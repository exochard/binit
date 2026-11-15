CORPUS_LOAD_ORDER="${CORPUS_LOAD_ORDER-}@@ID@@;"
export CORPUS_EXPORT_@@ID@@=ready
alias corpus_alias_@@ID@@=':'
corpus_fn_@@ID@@() {
    CORPUS_ARGC=$#
    CORPUS_ARGS=
    for corpus_arg do
        CORPUS_ARGS="${CORPUS_ARGS}<${corpus_arg}>"
    done
    CORPUS_CALLS=$(( ${CORPUS_CALLS:-0} + 1 ))
    cd -- "$CORPUS_TARGET" || return
    return 37
}
