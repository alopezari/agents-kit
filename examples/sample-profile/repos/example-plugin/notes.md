# example-plugin: notes

The repository's own instructions come first; these are additions.

## Checking your work
- `~/.agents/repos/example-plugin/verify` runs when you stop: the linters and the tests related to the change, then the kit's WordPress rules and this project's rules (`rules.semgrep.yml`) on changed lines.
- The full suite is `composer test`; run it before saying you're done.

## Traps already paid for
- Log through `Example_Plugin\Logger`, never `error_log()`: production silences the PHP error log.
