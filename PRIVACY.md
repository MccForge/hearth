# Hearth privacy policy

Hearth is a prototype built for the Amazon Developer Hackathon 2026. This policy describes how the software handles data when you run it.

**What Hearth stores.** The name, time zone, and check-in window of the person being looked after; the names and contact channels of the family members they list; medications and appointments entered by the family or the person; the answers given during check-ins, including the person's exact words; recordings the family and the person choose to leave for each other; and the alerts and notifications Hearth generates. All of it lives in a single SQLite database and a folder of audio files on the machine that runs Hearth.

**Where it goes.** Nowhere, unless you configure a channel. Email and webhook delivery are off by default and only send to the contacts you list. Hearth makes no calls to third-party services on its own. When Hearth is connected to Alexa+, Alexa+ receives the tool results it asks for during a conversation, under Amazon's own privacy terms.

**Account linking.** When an Alexa+ device is linked, Hearth issues an OAuth token bound to one person. Tools called with that token only ever return information about that person and only notify that person's listed contacts. Tokens can be revoked at any time by deleting them from the database or unlinking in Alexa+.

**What Hearth does not do.** It does not diagnose, does not give medical advice, does not sell or share data, does not synthesize anyone's voice, and does not record conversations beyond the answers and notes described above.

**Retention and deletion.** Data is kept until you delete it. Deleting the database file and the media folder removes everything.

**Contact.** Open an issue at https://github.com/MccForge/hearth.
