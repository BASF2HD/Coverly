# Coverly Artwork Handoff — Chrome / Edge

This optional, unpacked extension attaches only images explicitly selected in Coverly to a newly opened ChatGPT draft. It never clicks Send, reads account cookies or requests broad browsing-history permissions. Its persistent host permissions are limited to `http://localhost:8000` and `https://chatgpt.com`. Review the included source before installing.

1. Open Chrome `chrome://extensions` or Edge `edge://extensions`.
2. Enable Developer mode, choose **Load unpacked**, and select this `browser-bridge` directory. This is a local extension, not a store-reviewed extension.
3. Open Coverly at `http://localhost:8000` in that same browser/profile and reload once. Choose photos in Restoration studio, then **Attach selected images in ChatGPT**.
4. Sign in to ChatGPT if required. The extension waits for its file-input control. Verify attachment thumbnails and the prompt, then press Send yourself.

The Codex in-app browser cannot load this extension. Its fallback is **Copy image**, followed by paste in ChatGPT; no folder search is needed. Keep Coverly running until the handoff finishes. Handoff tokens expire after ten minutes and do not grant access to any other artwork. ChatGPT can change its DOM or reject programmatic uploads; the status says the files were offered to its upload control, not that server-side processing is complete. If no upload input is available, complete sign-in or use the clipboard fallback. No CAPTCHA is solved or bypassed.

The bridge is tested against a controlled attachment form. Live ChatGPT upload requires the user to install the extension; installation is never performed silently by Coverly or Codex.

## Return a reference page to Coverly (version 1.1)

Existing installations: open `chrome://extensions` / `edge://extensions`, click the extension's **Reload** button, then reload Coverly in the same browser profile. Version 1.1 adds `activeTab` and `scripting`: temporary access to the current page only when you explicitly invoke the extension. It does not add persistent host access to all websites.

In an album choose **Search album on the web**, then **Search Google + return page**, or connect an already-found page using **Connect this album to browser import**. Open a film or album reference result, click the Coverly extension icon and choose **Use this page in Coverly**. The selected page's headings, readable credits/tables and public album JSON-LD return to the connected local album as a suggestion. Review **Use these details**, then save. Requests expire after 30 minutes. Keep the original Coverly tab open.

This action does not submit the Google search page itself, copy cookies, inspect account state or read other tabs. Forms and executable scripts are excluded. Spotify and other sites may not expose complete readable credits in their current DOM; the copied-table and URL-import controls remain available. Captured performer credits are not automatically promoted to composer or singer roles. The ChatGPT draft attachment feature remains separate and never sends the draft.

## Connection recovery (version 1.2)

Opening an album now connects it automatically. The popup also checks an already-open Coverly album if its previous connection expired, and Open Coverly returns to the existing tab. There are no new permissions. After updating the local extension files, Reload it in chrome://extensions / edge://extensions and reload Coverly once. The selected reference remains read only after your explicit Use this page click. Selected pages can correct the archive's title; a differing title is flagged for review.
