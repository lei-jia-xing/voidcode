const MAX_PROMPT_TITLE_LENGTH = 56;
const MAX_PROMPT_TITLE_WORDS = 7;
const MAX_UNSPACED_TITLE_LENGTH = 24;
const MAX_CJK_TITLE_LENGTH = 18;
const CJK_PATTERN = /[\u3400-\u9fff]/u;

export function buildSessionDisplayTitle(
  prompt: string | null | undefined,
  sessionId: string,
): string {
  const promptTitle = summarizePrompt(prompt);
  if (promptTitle) return promptTitle;
  return sessionId.slice(0, 8) || sessionId;
}

function summarizePrompt(prompt: string | null | undefined): string | null {
  const normalized = prompt?.replace(/\s+/g, " ").trim();
  if (!normalized) return null;

  const firstClause = normalized.split(/[.!?。！？]/)[0]?.trim();
  const source = stripPromptBoilerplate(
    firstClause && firstClause.length >= 12 ? firstClause : normalized,
  );

  if (source.length <= MAX_PROMPT_TITLE_LENGTH) return source;

  if (CJK_PATTERN.test(source)) {
    return `${source.slice(0, MAX_CJK_TITLE_LENGTH).trim()}…`;
  }

  const words = source.split(" ").filter(Boolean);
  if (words.length <= 1) {
    return `${source.slice(0, MAX_UNSPACED_TITLE_LENGTH).trim()}…`;
  }

  const titleWords: string[] = [];
  for (const word of words) {
    const next = [...titleWords, word].join(" ");
    if (
      titleWords.length >= MAX_PROMPT_TITLE_WORDS ||
      next.length > MAX_PROMPT_TITLE_LENGTH
    ) {
      break;
    }
    titleWords.push(word);
  }

  return `${titleWords.join(" ")}…`;
}

function stripPromptBoilerplate(prompt: string): string {
  let title = prompt.trim();
  const patterns = [
    /^请你作为[^，,。！？]*[，,]\s*/u,
    /^请作为[^，,。！？]*[，,]\s*/u,
    /^作为[^，,。！？]*[，,]\s*/u,
    /^请你\s*/u,
    /^请\s*/u,
    /^帮我\s*/u,
    /^在(?:当前|这个|本)?仓库中/u,
    /^在(?:当前|这个|本)?项目中/u,
    /^(?:实现|创建|添加|编写|构建|完成|制作|做)(?:一个|一份|一下)?/u,
  ];

  let changed = true;
  while (changed) {
    changed = false;
    for (const pattern of patterns) {
      const next = title.replace(pattern, "").trim();
      if (next !== title) {
        title = next;
        changed = true;
      }
    }
  }

  return title || prompt.trim();
}
