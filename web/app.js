// SF Police Policy Explorer -- the browser side.
//
// 1. Send the question to POST /ask (app/main.py) and show a "thinking" message while waiting.
// 2. Show the answer. The answer is UNTRUSTED text (it comes from an LLM that read web
//    pages), so it is never inserted with innerHTML: if it contained "<script>" or
//    "<img onerror=...>", innerHTML would run it (an XSS attack). Instead we build the
//    elements ourselves and only ever put the answer's words into text nodes.
// 3. Show one card per cited source, and the thumbs up/down buttons (POST /feedback).

"use strict";

const form = document.getElementById("ask-form");
const textarea = document.getElementById("question");
const askButton = document.getElementById("ask-button");
const charCount = document.getElementById("char-count");
const chips = document.querySelectorAll(".chip");
const statusBox = document.getElementById("status");
const statusText = document.getElementById("status-text");
const errorBox = document.getElementById("error");
const resultBox = document.getElementById("result");
const answerBox = document.getElementById("answer");
const sourcesBlock = document.getElementById("sources-block");
const citationList = document.getElementById("citations");
const feedbackBox = document.getElementById("feedback");
const feedbackLabel = document.getElementById("feedback-label");
const thumbs = document.querySelectorAll(".thumb");

const MAX_LENGTH = 500; // Same limit the API enforces (app/main.py).
const LEGAL_NOTE = "This is not legal advice."; // Added by app/generate.py; shown smaller.

// [3], [1, 3] or [1,3] -- the same pattern as CITATION_PATTERN in app/generate.py.
// Combined with **bold** so one pass over a line finds both.
const INLINE_PATTERN = /\*\*(.+?)\*\*|\[(\d+(?:\s*,\s*\d+)*)\]/g;

// What to show while waiting, by seconds elapsed. Most answers take 2-7 s, but Render's
// free tier sleeps after 15 idle minutes and takes up to a minute to wake up.
const WAIT_MESSAGES = [
  [0, "Searching the policies…"],
  [4, "Still thinking… answers usually take a few seconds."],
  [15, "Taking longer than usual. If the site was asleep, the first answer can take up to a minute."],
];

let currentQueryId = null;
let busy = false;

// ---------- Asking ----------

async function ask(question) {
  question = question.trim();
  if (busy) return;
  if (question.length < 3) {
    showError("Please type a question (at least 3 characters).");
    return;
  }

  setBusy(true);
  errorBox.hidden = true;
  resultBox.hidden = true;
  const stopWaiting = showWaitMessages();

  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await response.json().catch(() => null); // Error pages may not be JSON.
    if (!response.ok) {
      showError(errorMessage(response.status, data));
      return;
    }
    showResult(data);
  } catch {
    // fetch() itself failed: no internet, or the server is down.
    showError("Couldn't reach the server. Check your connection and try again.");
  } finally {
    stopWaiting();
    setBusy(false);
  }
}

function errorMessage(status, data) {
  if (status === 422) return "Please enter a question between 3 and 500 characters.";
  // Our API's own errors (429 rate limit, 503 busy, ...) carry a readable "detail" string.
  if (data && typeof data.detail === "string") return data.detail;
  return `Something went wrong (error ${status}). Please try again.`;
}

function showWaitMessages() {
  const started = Date.now();
  const update = () => {
    const seconds = (Date.now() - started) / 1000;
    const current = WAIT_MESSAGES.filter(([after]) => seconds >= after).pop();
    statusText.textContent = current[1];
  };
  update();
  statusBox.hidden = false;
  const timer = setInterval(update, 1000);
  return () => {
    clearInterval(timer);
    statusBox.hidden = true;
  };
}

function setBusy(value) {
  busy = value;
  askButton.disabled = value;
  chips.forEach((chip) => (chip.disabled = value));
  askButton.textContent = value ? "Asking…" : "Ask";
}

function showError(message) {
  errorBox.textContent = message; // textContent, never innerHTML.
  errorBox.hidden = false;
}

// ---------- Showing the answer ----------

function showResult(data) {
  const cited = new Set(data.citations.map((c) => c.n));
  renderAnswer(answerBox, data.answer, cited);
  renderCitations(data.citations);
  sourcesBlock.hidden = data.citations.length === 0;

  currentQueryId = data.query_id;
  feedbackBox.hidden = currentQueryId === null; // Logging failed: nothing to rate.
  feedbackLabel.textContent = "Was this answer helpful?";
  thumbs.forEach((thumb) => {
    thumb.disabled = false;
    thumb.setAttribute("aria-pressed", "false");
  });

  resultBox.hidden = false;
  resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Turns the model's plain text (with a little Markdown) into paragraphs and lists.
function renderAnswer(container, text, cited) {
  container.replaceChildren();
  let list = null; // The <ul>/<ol> that bullet lines are currently being added to.

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line) {
      list = null; // A blank line ends a list.
      continue;
    }

    const item = line.match(/^([-*•]|\d+[.)])\s+(.*)$/); // "- x", "* x", "1. x", "1) x"
    if (item) {
      const tag = /^\d/.test(item[1]) ? "OL" : "UL";
      if (!list || list.tagName !== tag) {
        list = document.createElement(tag);
        container.append(list);
      }
      const li = document.createElement("li");
      appendInline(li, item[2], cited);
      list.append(li);
      continue;
    }

    list = null;
    const p = document.createElement("p");
    const heading = line.match(/^#{1,6}\s+(.*)$/); // "### Heading" -> bold paragraph
    if (heading) {
      const strong = document.createElement("strong");
      appendInline(strong, heading[1], cited);
      p.append(strong);
    } else {
      appendInline(p, line, cited);
    }
    if (line === LEGAL_NOTE) p.className = "legal-note";
    container.append(p);
  }
}

// Adds one line's text to `parent`, turning **bold** into <strong> and [n] into links.
function appendInline(parent, text, cited) {
  let last = 0;
  for (const match of text.matchAll(INLINE_PATTERN)) {
    parent.append(text.slice(last, match.index)); // Plain text before the match.
    if (match[1] !== undefined) {
      const strong = document.createElement("strong");
      appendInline(strong, match[1], cited); // Bold text can contain citations.
      parent.append(strong);
    } else {
      parent.append(citationMarker(match[2], cited));
    }
    last = match.index + match[0].length;
  }
  parent.append(text.slice(last));
}

// "[1, 3]" -> a small "[1, 3]" where 1 and 3 link to their source cards.
function citationMarker(numbers, cited) {
  const span = document.createElement("span");
  span.className = "cite";
  span.append("[");
  numbers.split(",").forEach((raw, i) => {
    const n = Number(raw.trim());
    if (i > 0) span.append(", ");
    if (cited.has(n)) {
      const a = document.createElement("a");
      a.href = `#source-${n}`;
      a.textContent = String(n);
      a.setAttribute("aria-label", `Source ${n}`);
      span.append(a);
    } else {
      span.append(String(n)); // A number with no matching source: shown, not linked.
    }
  });
  span.append("]");
  return span;
}

function renderCitations(citations) {
  citationList.replaceChildren(
    ...citations.map((c) => {
      const li = document.createElement("li");
      li.className = "card";
      li.id = `source-${c.n}`; // The target of the [n] links in the answer.

      const badge = element("span", "badge", `[${c.n}]`);
      const title = element("div", "card-title", `${c.document_id.replace("-", " ")} · ${c.title}`);
      const section = element("div", "card-section", c.section || "Introduction");
      const meta = element("div", "card-meta", capitalize(c.date));

      // Only link to real web addresses (a "javascript:" URL would run code when clicked).
      if (/^https:\/\//.test(c.url)) {
        const link = element("a", "", "Read the official policy ↗");
        link.href = c.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        meta.append(" · ", link);
      }
      li.append(badge, title, section, meta);
      return li;
    }),
  );
}

function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  return el;
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// ---------- Feedback ----------

async function sendFeedback(button) {
  if (currentQueryId === null) return;
  const rating = Number(button.dataset.rating);
  thumbs.forEach((thumb) => (thumb.disabled = true));

  try {
    const response = await fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query_id: currentQueryId, rating }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    button.setAttribute("aria-pressed", "true");
    feedbackLabel.textContent = "Thanks for the feedback!";
  } catch {
    feedbackLabel.textContent = "Couldn't send feedback. Please try again.";
    thumbs.forEach((thumb) => (thumb.disabled = false));
  }
}

// ---------- Wiring up the page ----------

form.addEventListener("submit", (event) => {
  event.preventDefault(); // Stop the browser's default full-page form submit.
  ask(textarea.value);
});

// Enter asks; Shift+Enter adds a new line.
textarea.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

textarea.addEventListener("input", () => {
  charCount.textContent = `${textarea.value.length} / ${MAX_LENGTH}`;
});

chips.forEach((chip) =>
  chip.addEventListener("click", () => {
    textarea.value = chip.textContent.trim();
    textarea.dispatchEvent(new Event("input")); // Update the character count.
    ask(textarea.value);
  }),
);

thumbs.forEach((thumb) => thumb.addEventListener("click", () => sendFeedback(thumb)));
