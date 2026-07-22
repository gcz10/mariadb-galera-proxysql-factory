# Profile — do uzupełnienia w F2; utworzone inkrementalnie, gdy realizują aktywny feature.

# production:
#   versions_policy: locked
#   tls_mode: full   # lub disabled + risk acceptance
#   backup_destination: s3
#   chaos_tests: false
#
# staging:
#   versions_policy: candidate
#   tls_mode: full
#   backup_destination: smb
#   chaos_tests: true
#
# laboratory:
#   versions_policy: candidate
#   tls_mode: disabled
#   backup_destination: smb
#   chaos_tests: true
