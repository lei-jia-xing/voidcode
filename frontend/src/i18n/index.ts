import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enLocales from "./locales/en.json";
import zhCNLocales from "./locales/zh-CN.json";

const resources = {
  en: {
    translation: enLocales,
  },
  "zh-CN": {
    translation: zhCNLocales,
  },
};

function initialLanguage(): "en" | "zh-CN" {
  if (typeof window === "undefined") return "en";
  try {
    const persisted = window.localStorage.getItem("app-storage");
    const parsed = persisted
      ? (JSON.parse(persisted) as { state?: { language?: string } })
      : null;
    return parsed?.state?.language === "zh-CN" ? "zh-CN" : "en";
  } catch {
    return "en";
  }
}

i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage(),
  fallbackLng: "en",
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
