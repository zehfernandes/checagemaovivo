export const adminPassword = () => localStorage.getItem('admin') ?? ''
export const adminHeaders = (): Record<string, string> => ({ 'x-admin-password': adminPassword() })
