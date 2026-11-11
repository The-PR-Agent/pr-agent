"""Shell-style splitting of configured commands, shared by the dispatcher and the servers."""


def split_command_words(command: str) -> list[str]:
    """Split a configured command on the boundaries ``parse_command`` uses, keeping each word's quoting.

    Raises ValueError when ``parse_command`` would reject the command.
    """
    return _split_command(command, raw=True)


def _split_command(command: str, *, single_quotes: bool = True, raw: bool = False):
    """Split an auto command and retain whether each token was quoted.

    ``shlex.split`` removes quote markers before setting overrides are handed to
    ``yaml.safe_load``. That makes a quoted ``#`` look like a YAML comment and
    changes quoted scalar values such as ``"true"`` into booleans. This small
    tokenizer keeps the normal shell-style token boundaries while recording the
    presence of quotes so setting values can be normalized as strings later.

    Apostrophes inside a word remain literal, matching the legacy request parser
    (for example, ``What's``). An apostrophe at a token boundary or immediately
    after ``=`` still starts a single-quoted value, as used by the documented
    webhook configuration examples. Raw request parsing disables single quotes
    to match its shlex lexer.
    """
    tokens = []
    token = []
    quote = None
    value_was_quoted = False
    equals_seen = False
    token_started = False
    raw_start = None

    def flush_token():
        nonlocal equals_seen, token_started, token, value_was_quoted, raw_start
        if token_started:
            tokens.append(command[raw_start:index] if raw else ("".join(token), value_was_quoted))
        token = []
        equals_seen = False
        token_started = False
        value_was_quoted = False
        raw_start = None

    index = 0
    while index < len(command):
        character = command[index]
        if raw_start is None and not character.isspace():
            raw_start = index
        if quote is None:
            if character.isspace():
                flush_token()
            elif character == "\\":
                if index + 1 >= len(command):
                    raise ValueError("No escaped character")
                token.append(command[index + 1])
                token_started = True
                index += 1
            elif character == "=":
                token.append(character)
                equals_seen = True
                token_started = True
            elif character == '"':
                quote = character
                value_was_quoted = equals_seen
                token_started = True
            elif single_quotes and character == "'" and (not token_started or command[index - 1] == "="):
                quote = character
                value_was_quoted = equals_seen
                token_started = True
            else:
                token.append(character)
                token_started = True
        elif quote == "'":
            if character == "'":
                quote = None
            else:
                token.append(character)
        else:
            if character == '"':
                quote = None
            elif character == "\\":
                if index + 1 >= len(command):
                    raise ValueError("No escaped character")
                escaped = command[index + 1]
                if escaped in {'"', "\\", "$", "`"}:
                    token.append(escaped)
                elif escaped != "\n":
                    token.extend(("\\", escaped))
                index += 1
            else:
                token.append(character)
        index += 1

    if quote is not None:
        raise ValueError("No closing quotation")
    flush_token()
    return tokens
