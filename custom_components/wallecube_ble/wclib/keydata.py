"""
Static key material for deriving the BLE session key

The 123-byte constant is compiled into the W150 front-panel firmware. Together with the
device's factory MAC address it yields the per-device AES key, see
`encryption.derive_session_key`.
"""

SESSION_SECRET = bytes.fromhex(
    "4875694855494455484430573862696c7539776572686430485530716877686f"
    "7569232829292140265968647130686a6f4023554f484e6a6e696a6f68716577"
    "7564686a6a6e4f4f4853444f553839323833262a695f2968692a6f2674742a76"
    "6926745e2624444643495547383659465954547426696626242373"
)
