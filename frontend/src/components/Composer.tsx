import { useState, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Send, Loader2 } from 'lucide-react';

interface ComposerProps {
  disabled: boolean;
  isRunning: boolean;
  placeholder?: string;
  onSubmit: (message: string) => void;
}

export function Composer({ disabled, isRunning, placeholder, onSubmit }: ComposerProps) {
  const { t } = useTranslation();
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);

  const handleSubmit = () => {
    const trimmed = input.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setInput('');
    const el = textareaRef.current;
    if (el) el.style.height = 'auto';
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    resizeTextarea();
  };

  return (
    <div className="border-t border-slate-800 bg-[#0c0c0e] px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="relative flex items-end gap-2 bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500 transition-colors">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder={placeholder || t('chat.placeholder')}
            disabled={disabled}
            rows={1}
            className="flex-1 bg-transparent text-sm text-slate-200 placeholder:text-slate-500 resize-none outline-none py-1.5 max-h-[200px] disabled:opacity-50"
          />
          <button
            type="button"
            onClick={handleSubmit}
            disabled={disabled || !input.trim()}
            className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors mb-0.5"
          >
            {isRunning ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          </button>
        </div>
        <p className="text-[11px] text-slate-600 mt-1.5 text-center">
          {t('chat.hint')}
        </p>
      </div>
    </div>
  );
}
