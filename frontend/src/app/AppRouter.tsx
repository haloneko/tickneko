/** 路由表：/login 公开；其余在登录守卫 + 后台布局之内。 */
import { Routes, Route, Navigate } from 'react-router-dom'
import RequireAuth from './RequireAuth'
import AppLayout from '../features/layout/AppLayout'
import LoginPage from '../features/auth/LoginPage'
import RegisterPage from '../features/auth/RegisterPage'
import DashboardPage from '../features/dashboard/DashboardPage'
import SessionsPage from '../features/sessions/SessionsPage'
import TokensPage from '../features/tokens/TokensPage'
import LogsPage from '../features/logs/LogsPage'
import DebugPage from '../features/onebot/DebugPage'
import WorkflowPage from '../features/workflow/WorkflowPage'
import VariablesPage from '../features/variables/VariablesPage'
import ProfilePage from '../features/auth/ProfilePage'

export default function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      <Route element={<RequireAuth />}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/bots" element={<TokensPage />} />
          <Route path="/workflows" element={<WorkflowPage />} />
          <Route path="/variables" element={<VariablesPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/debug" element={<DebugPage />} />
          <Route path="/profile" element={<ProfilePage />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
