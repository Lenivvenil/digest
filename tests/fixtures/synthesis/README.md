# Synthetic v1 compatibility fixtures

These files were created offline by the engine at
`018d00ee8f60a232c1d1571ca36b089825b4e081` (tree
`4ca7e355627c3f6be6fdd845587f6afd5fc9a90b`) before changing the comparison
schema. They contain invented sources, example.com URLs and test-only recipient
identifiers. They are not runtime evidence or generated semantic acceptance.

- `v1_attempts.json`: complete schema-1/prompt-v1 reserved, valid and failed
  attempt records, with their original request/response/result hashes.
- `v1_prepared_edition.json`: an actual synthetic frozen manifest containing the
  v1 rendered block/result; tests claim and send it through mocked transport
  without using the v2 parser or creating a synthesis sidecar.

Keep these fixtures unchanged when testing later schema/prompt changes. A new
prompt must never silently reopen the same accepted preparation or rewrite its
frozen reader payload.
