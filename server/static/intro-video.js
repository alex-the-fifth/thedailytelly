/* No analytics or third-party player. Fetch the recording only on request. */
(() => {
  const button = document.getElementById('intro-play');
  const video = document.getElementById('intro-video');
  const status = document.getElementById('intro-status');
  if (!button || !video) return;
  button.hidden = false;
  button.addEventListener('click', () => {
    if (!video.src) video.src = video.dataset.src;
    button.hidden = true;
    video.hidden = false;
    video.focus({ preventScroll: true });
    video.play().catch(() => {
      status.textContent = 'Press play in the video controls to begin, or use the link below.';
      status.hidden = false;
    });
  });
  video.addEventListener('error', () => {
    status.textContent = 'The video could not load. You can read the transcript below or open the video directly.';
    status.hidden = false;
  });
})();
