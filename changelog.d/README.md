# Changelog entries

Each pull request adds one file here, named after its branch (`feature~name.md` for `feature/name`), with one or more lines starting with `- `: what changes for someone using the kit. Leave out the pull request number; the release adds it. CI checks the file is there, unless the pull request is labeled "no changelog".

`bin/changelog release <version>` moves these lines into CHANGELOG.md and deletes the files.
