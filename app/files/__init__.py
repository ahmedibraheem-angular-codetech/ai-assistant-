"""Read-only file intelligence.

A workspace is a directory the user explicitly registers. File tools can read
only inside registered roots, and only through the isolated worker. They do
not write, delete, or execute anything.

The API has no authentication. It must stay on localhost. A workspace id is
not a security boundary: any agent granted a file tool can read every enabled
workspace. Authentication and authorization are required before remote access.

macOS privacy protections still apply. A permission denial is returned as an
error. This package does not try to bypass them.

Future write, command, or external-tool capabilities would be new tools and a
new access scope. They are not implemented here.
"""
