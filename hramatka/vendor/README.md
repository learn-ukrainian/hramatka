# Pinned public contracts (vendored)

All files under hramatka/vendor/ are immutable, digest-pinned copies of public contracts.
Do NOT hand-edit anything in this directory.

Re-vendoring flow:
1. Make and merge the contract change in the PUBLIC repository first.
2. Copy the new contract file and regenerate digests in `MANIFEST.json`.
3. Reference the public-repo commit in a commit message using the `Revendor:` trailer.

Example commit message:
  Update activity JSON schema.
  Revendor: learn-ukrainian-contract@ffd54054
