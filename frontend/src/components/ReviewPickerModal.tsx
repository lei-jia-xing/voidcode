import { useState, useEffect, useMemo, useRef } from "react";
import {
  X,
  Search,
  GitBranch,
  GitCommit,
  GitPullRequest,
  FileCode,
  PenTool,
  ArrowLeft,
  LoaderCircle,
} from "lucide-react";
import { RuntimeClient } from "../lib/runtime/client";
import { ControlButton } from "./ui";

export interface ReviewTargetPayload {
  type: "uncommitted" | "base_branch" | "commit" | "custom";
  branch?: string;
  sha?: string;
  title?: string;
  instructions?: string;
}

interface ReviewPickerModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSelectTarget: (target: ReviewTargetPayload) => void;
}

type Step = "preset" | "base_branch" | "commit" | "custom";

export function ReviewPickerModal({
  isOpen,
  onClose,
  onSelectTarget,
}: ReviewPickerModalProps) {
  const [step, setStep] = useState<Step>("preset");
  const [query, setQuery] = useState("");
  const [customInstructions, setCustomInstructions] = useState("");

  const [branches, setBranches] = useState<string[]>([]);
  const [currentBranch, setCurrentBranch] = useState<string | null>(null);
  const [branchesLoading, setBranchesLoading] = useState(false);
  const [branchesError, setBranchesError] = useState<string | null>(null);

  const [commits, setCommits] = useState<
    Array<{ sha: string; timestamp: number; subject: string }>
  >([]);
  const [commitsLoading, setCommitsLoading] = useState(false);
  const [commitsError, setCommitsError] = useState<string | null>(null);

  const dialogRef = useRef<HTMLDivElement>(null);

  const loadBranches = () => {
    setBranchesLoading(true);
    setBranchesError(null);
    RuntimeClient.reviewBranches()
      .then((res) => {
        setBranches(res.branches);
        setCurrentBranch(res.current);
      })
      .catch((err) => {
        setBranchesError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        setBranchesLoading(false);
      });
  };

  const loadCommits = () => {
    setCommitsLoading(true);
    setCommitsError(null);
    RuntimeClient.reviewCommits(100)
      .then((res) => {
        setCommits(res.commits);
      })
      .catch((err) => {
        setCommitsError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        setCommitsLoading(false);
      });
  };

  const handleClose = () => {
    setStep("preset");
    setQuery("");
    setCustomInstructions("");
    setBranchesError(null);
    setCommitsError(null);
    onClose();
  };

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (!isOpen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        if (step !== "preset") {
          setStep("preset");
          setQuery("");
        } else {
          handleClose();
        }
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  });

  const filteredBranches = useMemo(() => {
    const q = query.toLowerCase().trim();
    if (!q) return branches;
    return branches.filter((b) => b.toLowerCase().includes(q));
  }, [branches, query]);

  const filteredCommits = useMemo(() => {
    const q = query.toLowerCase().trim();
    if (!q) return commits;
    return commits.filter(
      (c) =>
        c.subject.toLowerCase().includes(q) || c.sha.toLowerCase().includes(q),
    );
  }, [commits, query]);

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Select a review preset"
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xs animate-in fade-in duration-150"
    >
      <div
        ref={dialogRef}
        className="w-full max-w-lg bg-(--color-background-primary) border border-(--color-border-subtle) rounded-xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh] animate-in zoom-in-95 duration-150 text-(--color-text-primary)"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-(--color-border-subtle)">
          <div className="flex items-center gap-2">
            {step !== "preset" && (
              <button
                type="button"
                onClick={() => {
                  setStep("preset");
                  setQuery("");
                }}
                className="p-1 rounded-md text-(--color-text-muted) hover:text-(--color-text-primary) hover:bg-(--color-background-hover) transition-colors"
                aria-label="Back"
              >
                <ArrowLeft className="w-4 h-4" />
              </button>
            )}
            <h2 className="text-base font-semibold">
              {step === "preset" && "Select a review preset"}
              {step === "base_branch" && "Select a base branch"}
              {step === "commit" && "Select a commit to review"}
              {step === "custom" && "Custom review instructions"}
            </h2>
          </div>
          <button
            type="button"
            onClick={handleClose}
            className="p-1.5 rounded-lg text-(--color-text-muted) hover:text-(--color-text-primary) hover:bg-(--color-background-hover) transition-colors"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {step === "preset" && (
          <div className="p-3 space-y-1">
            <button
              type="button"
              onClick={() => {
                setStep("base_branch");
                setQuery("");
                loadBranches();
              }}
              className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-left text-sm hover:bg-(--color-background-hover) transition-colors group"
            >
              <GitPullRequest className="w-4 h-4 text-(--color-accent-primary)" />
              <div className="flex-1">
                <div className="font-medium">Review against a base branch</div>
                <div className="text-xs text-(--color-text-muted)">
                  (PR Style)
                </div>
              </div>
            </button>

            <button
              type="button"
              onClick={() => {
                handleClose();
                onSelectTarget({ type: "uncommitted" });
              }}
              className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-left text-sm hover:bg-(--color-background-hover) transition-colors group"
            >
              <FileCode className="w-4 h-4 text-(--color-accent-primary)" />
              <div className="flex-1">
                <div className="font-medium">Review uncommitted changes</div>
              </div>
            </button>

            <button
              type="button"
              onClick={() => {
                setStep("commit");
                setQuery("");
                loadCommits();
              }}
              className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-left text-sm hover:bg-(--color-background-hover) transition-colors group"
            >
              <GitCommit className="w-4 h-4 text-(--color-accent-primary)" />
              <div className="flex-1">
                <div className="font-medium">Review a commit</div>
              </div>
            </button>

            <button
              type="button"
              onClick={() => {
                setStep("custom");
                setQuery("");
              }}
              className="w-full flex items-center gap-3 px-4 py-3 rounded-lg text-left text-sm hover:bg-(--color-background-hover) transition-colors group"
            >
              <PenTool className="w-4 h-4 text-(--color-accent-primary)" />
              <div className="flex-1">
                <div className="font-medium">Custom review instructions</div>
              </div>
            </button>
          </div>
        )}

        {step === "base_branch" && (
          <div className="flex flex-col flex-1 p-3 overflow-hidden">
            <div className="relative mb-3">
              <Search className="absolute left-3 top-2.5 w-4 h-4 text-(--color-text-muted)" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Type to search branches"
                autoFocus
                className="w-full pl-9 pr-3 py-2 text-sm bg-(--color-background-secondary) border border-(--color-border-subtle) rounded-lg focus:outline-hidden focus:border-(--color-accent-primary) text-(--color-text-primary)"
              />
            </div>

            <div className="flex-1 overflow-y-auto space-y-1 max-h-72">
              {branchesLoading && (
                <div className="flex items-center justify-center py-6 text-sm text-(--color-text-muted)">
                  <LoaderCircle className="w-4 h-4 animate-spin mr-2" />
                  Loading branches...
                </div>
              )}
              {branchesError && (
                <div className="py-4 px-3 text-sm text-red-500">
                  {branchesError}
                </div>
              )}
              {!branchesLoading &&
                !branchesError &&
                filteredBranches.length === 0 && (
                  <div className="py-6 text-center text-sm text-(--color-text-muted)">
                    No matching branches found.
                  </div>
                )}
              {!branchesLoading &&
                filteredBranches.map((branch) => (
                  <button
                    key={branch}
                    type="button"
                    onClick={() => {
                      handleClose();
                      onSelectTarget({ type: "base_branch", branch });
                    }}
                    className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-left text-sm hover:bg-(--color-background-hover) transition-colors"
                  >
                    <GitBranch className="w-3.5 h-3.5 text-(--color-text-muted)" />
                    <span className="font-medium text-(--color-text-primary)">
                      {currentBranch || "(detached HEAD)"}
                    </span>
                    <span className="text-xs text-(--color-text-muted)">
                      -&gt;
                    </span>
                    <span className="text-sm text-(--color-accent-primary)">
                      {branch}
                    </span>
                  </button>
                ))}
            </div>
          </div>
        )}

        {step === "commit" && (
          <div className="flex flex-col flex-1 p-3 overflow-hidden">
            <div className="relative mb-3">
              <Search className="absolute left-3 top-2.5 w-4 h-4 text-(--color-text-muted)" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Type to search commits"
                autoFocus
                className="w-full pl-9 pr-3 py-2 text-sm bg-(--color-background-secondary) border border-(--color-border-subtle) rounded-lg focus:outline-hidden focus:border-(--color-accent-primary) text-(--color-text-primary)"
              />
            </div>

            <div className="flex-1 overflow-y-auto space-y-1 max-h-72">
              {commitsLoading && (
                <div className="flex items-center justify-center py-6 text-sm text-(--color-text-muted)">
                  <LoaderCircle className="w-4 h-4 animate-spin mr-2" />
                  Loading commits...
                </div>
              )}
              {commitsError && (
                <div className="py-4 px-3 text-sm text-red-500">
                  {commitsError}
                </div>
              )}
              {!commitsLoading &&
                !commitsError &&
                filteredCommits.length === 0 && (
                  <div className="py-6 text-center text-sm text-(--color-text-muted)">
                    No matching commits found.
                  </div>
                )}
              {!commitsLoading &&
                filteredCommits.map((commit) => (
                  <button
                    key={commit.sha}
                    type="button"
                    onClick={() => {
                      handleClose();
                      onSelectTarget({
                        type: "commit",
                        sha: commit.sha,
                        title: commit.subject,
                      });
                    }}
                    className="w-full flex items-center justify-between gap-3 px-3 py-2 rounded-md text-left text-sm hover:bg-(--color-background-hover) transition-colors"
                  >
                    <div className="flex items-center gap-2 truncate">
                      <GitCommit className="w-3.5 h-3.5 text-(--color-text-muted) shrink-0" />
                      <span className="truncate">{commit.subject}</span>
                    </div>
                    <span className="font-mono text-xs text-(--color-text-muted) shrink-0">
                      {commit.sha.slice(0, 7)}
                    </span>
                  </button>
                ))}
            </div>
          </div>
        )}

        {step === "custom" && (
          <div className="flex flex-col p-4 space-y-4">
            <div>
              <label
                htmlFor="custom-review-instructions"
                className="block text-xs font-medium text-(--color-text-muted) mb-1.5"
              >
                Instructions
              </label>
              <textarea
                id="custom-review-instructions"
                rows={4}
                value={customInstructions}
                onChange={(e) => setCustomInstructions(e.target.value)}
                placeholder="Type instructions and press Enter"
                autoFocus
                className="w-full p-3 text-sm bg-(--color-background-secondary) border border-(--color-border-subtle) rounded-lg focus:outline-hidden focus:border-(--color-accent-primary) text-(--color-text-primary) resize-none"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (customInstructions.trim()) {
                      handleClose();
                      onSelectTarget({
                        type: "custom",
                        instructions: customInstructions.trim(),
                      });
                    }
                  }
                }}
              />
            </div>
            <div className="flex justify-end gap-2">
              <ControlButton
                variant="secondary"
                onClick={() => {
                  setStep("preset");
                }}
              >
                Back
              </ControlButton>
              <ControlButton
                variant="primary"
                disabled={!customInstructions.trim()}
                onClick={() => {
                  if (customInstructions.trim()) {
                    handleClose();
                    onSelectTarget({
                      type: "custom",
                      instructions: customInstructions.trim(),
                    });
                  }
                }}
              >
                Review
              </ControlButton>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
