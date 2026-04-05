
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useAppStore } from './store';
import { Activity, Play, Settings, Code2, LayoutDashboard, CheckCircle2, Circle, Clock, Globe } from 'lucide-react';
import { RuntimeDebug } from './components/RuntimeDebug';

function App() {
  const { tasks, activities, language, setLanguage } = useAppStore();
  const { t, i18n } = useTranslation();

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  const toggleLanguage = () => {
    setLanguage(language === 'en' ? 'zh-CN' : 'en');
  };

  return (
    <div className="flex h-screen bg-[#09090b] text-slate-300 font-sans overflow-hidden selection:bg-indigo-500/30">

      {/* Sidebar Navigation */}
      <aside className="w-16 md:w-64 border-r border-slate-800 bg-[#09090b] flex flex-col justify-between">
        <div>
          <div className="h-16 flex items-center justify-center md:justify-start md:px-6 border-b border-slate-800 text-indigo-400 font-bold tracking-tight">
            <Code2 className="w-6 h-6 md:mr-3" />
            <span className="hidden md:block text-lg">{t('app.title')}</span>
          </div>

          <nav className="p-3 space-y-2">
            {[
              { icon: LayoutDashboard, label: t('nav.workspace'), active: true },
              { icon: Activity, label: t('nav.activity') },
            ].map((item) => (
              <button key={item.label} type="button" className={`w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg transition-colors ${item.active ? 'bg-indigo-500/10 text-indigo-400' : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`}>
                <item.icon className="w-5 h-5 md:mr-3" />
                <span className="hidden md:block font-medium">{item.label}</span>
              </button>
            ))}
          </nav>
        </div>

        <div className="p-3 border-t border-slate-800 space-y-2">
           <button type="button" onClick={toggleLanguage} className="w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors">
             <Globe className="w-5 h-5 md:mr-3" />
             <span className="hidden md:block font-medium">{language === 'en' ? t('language.zh') : t('language.en')}</span>
           </button>
           <RuntimeDebug />
           <button type="button" className="w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors">
             <Settings className="w-5 h-5 md:mr-3" />
             <span className="hidden md:block font-medium">{t('nav.settings')}</span>
           </button>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col min-w-0">

        {/* Header */}
        <header className="h-16 flex items-center justify-between px-6 border-b border-slate-800 bg-[#0c0c0e]">
          <h1 className="text-xl font-semibold text-slate-100">{t('session.current')}</h1>
          <div className="flex items-center space-x-4">
             <span className="px-3 py-1 rounded-full bg-emerald-500/10 text-emerald-400 text-xs font-medium border border-emerald-500/20 flex items-center">
               <span className="w-2 h-2 rounded-full bg-emerald-500 mr-2 animate-pulse"></span>
               {t('session.agentIdle')}
             </span>
             <button type="button" className="bg-indigo-600 hover:bg-indigo-500 text-white px-4 py-1.5 rounded-md text-sm font-medium transition-colors flex items-center shadow-sm shadow-indigo-500/20">
               <Play className="w-4 h-4 mr-2" />
               {t('task.newTask')}
             </button>
          </div>
        </header>

        {/* Workspace Layout */}
        <div className="flex-1 flex overflow-hidden bg-[#0a0a0c]">

          {/* Editor/Conversation Area */}
          <div className="flex-1 p-6 overflow-y-auto">
            <div className="max-w-4xl mx-auto space-y-6">
              <div className="bg-slate-800/30 border border-slate-700/50 rounded-xl p-6 shadow-sm">
                 <h2 className="text-lg font-medium text-slate-200 mb-4">{t('task.queue')}</h2>
                 <div className="space-y-3">
                   {tasks.map((task) => (
                     <div key={task.id} className="flex items-center justify-between p-4 bg-slate-900/50 rounded-lg border border-slate-800 hover:border-slate-700 transition-colors">
                       <div className="flex items-center space-x-4">
                         {task.status === 'completed' ? (
                           <CheckCircle2 className="w-5 h-5 text-emerald-500" />
                         ) : task.status === 'in_progress' ? (
                           <Clock className="w-5 h-5 text-indigo-400" />
                         ) : (
                           <Circle className="w-5 h-5 text-slate-500" />
                         )}
                         <span className="text-slate-300 font-medium">{task.title}</span>
                       </div>
                       <span className={`text-xs px-2.5 py-1 rounded-md font-medium capitalize border ${
                         task.status === 'in_progress' ? 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20' :
                         task.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' :
                         'bg-slate-800 text-slate-400 border-slate-700'
                       }`}>
                         {t(`task.status.${task.status}`)}
                       </span>
                     </div>
                   ))}
                 </div>
              </div>

              <div className="bg-slate-800/30 border border-slate-700/50 rounded-xl p-6 shadow-sm min-h-[400px] flex flex-col items-center justify-center text-center">
                <Code2 className="w-12 h-12 text-slate-600 mb-4" />
                <h3 className="text-xl font-medium text-slate-300 mb-2">{t('editor.noActiveFile')}</h3>
                <p className="text-slate-500 max-w-sm">
                  {t('editor.selectTask')}
                </p>
              </div>
            </div>
          </div>

          {/* Activity Panel */}
          <aside className="w-80 border-l border-slate-800 bg-[#0c0c0e] hidden lg:flex flex-col">
             <div className="p-4 border-b border-slate-800">
               <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">{t('activity.log')}</h3>
             </div>
             <div className="flex-1 overflow-y-auto p-4 space-y-4">
               {activities.map((activity) => (
                 <div key={activity.id} className="relative pl-4 border-l border-slate-800">
                   <div className="absolute -left-1.5 top-1.5 w-3 h-3 rounded-full bg-slate-800 border-2 border-[#0c0c0e]"></div>
                   <p className="text-sm text-slate-300 mb-1">{activity.message}</p>
                   <span className="text-xs text-slate-500">
                     {new Date(activity.timestamp).toLocaleTimeString()}
                   </span>
                 </div>
               ))}
             </div>
          </aside>

        </div>
      </main>
    </div>
  );
}

export default App;
