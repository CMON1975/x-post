# Bash completion for x-post: its options, queue ids after the commands that take one, logged
# post ids after --delete, and files after -i. Needs only bash's own `complete`, not the
# bash-completion package, so ~/.bashrc can source it directly.

_x_post() {
    local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]}
    COMPREPLY=()
    case $prev in
        --cancel|--retry|--move|--revise)
            local queue=${XDG_STATE_HOME:-$HOME/.local/state}/x-post/scheduled entry ids=()
            for entry in "$queue"/*/post.json; do
                [[ -e $entry ]] || continue  # an empty queue leaves the pattern itself
                entry=${entry%/post.json}
                ids+=("${entry##*/}")
            done
            mapfile -t COMPREPLY < <(compgen -W "${ids[*]}" -- "$cur")
            return ;;
        --delete)
            # Read newest first, so a post's delete (always later in the log) is met before it.
            # JSON escapes quotes inside a post's text, so only the real "id" key can match.
            local log=${XDG_STATE_HOME:-$HOME/.local/state}/x-post/log.jsonl line ids=() gone=" "
            [[ -r $log ]] && while IFS= read -r line; do
                [[ $line =~ \"kind\":\ \"([a-z ]+)\".*\"id\":\ \"([0-9]+)\" ]] || continue
                if [[ ${BASH_REMATCH[1]} == delete ]]; then
                    gone+="${BASH_REMATCH[2]} "
                elif [[ $gone != *" ${BASH_REMATCH[2]} "* ]]; then
                    ids+=("${BASH_REMATCH[2]}")
                fi
            done < <(tac "$log")
            compopt -o nosort 2>/dev/null  # keep newest first
            mapfile -t COMPREPLY < <(compgen -W "${ids[*]}" -- "$cur")
            return ;;
        -i|--image)
            compopt -o filenames 2>/dev/null  # slashes after folders, quoting for spaces
            mapfile -t COMPREPLY < <(compgen -f -- "$cur")
            return ;;
    esac
    if [[ $cur == -* ]]; then  # anything else (a message, a time, alt text) is free text
        mapfile -t COMPREPLY < <(compgen -W "-h --help -e --edit --undo --numbered --check -i --image -s --screenshot
            --alt --poll --poll-for --at --scheduled --cancel --retry --move --revise --send-due --spent --history --delete" -- "$cur")
    fi
}

complete -F _x_post x-post
