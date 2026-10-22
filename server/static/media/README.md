# Homepage introduction

The versioned MP4, JPEG poster, and WebVTT captions are served from this directory.
The homepage sets the video source only when the visitor presses Play. Playback
uses the browser's controls, English captions, and an expandable transcript, with
no third-party player or viewing analytics. A native player also works without
JavaScript.

The current recording was supplied by the project owner. Its complete spoken
performance is preserved in the introduction. It includes the parent and kids
experiences and names Android, Google TV, iOS, and the web. The homepage notes
that this is a launch preview while the iOS app is in development.

For host deployments, use the `/media/` location in the Nginx example. Public
media can be cached and served with byte ranges without passing through the
application. Keep media filenames versioned when replacing a video, poster, or
caption file. Household libraries and databases must never be placed here.
