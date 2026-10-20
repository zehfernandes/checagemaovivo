export const adminPassword = () => localStorage.getItem('admin') ?? ''
// who is reviewing in this browser (the admin password is shared by the newsroom)
export const reviewerName = () => localStorage.getItem('reviewer') ?? ''
export const setReviewerName = (name: string) => localStorage.setItem('reviewer', name.trim())
export const adminHeaders = (): Record<string, string> => ({ 'x-admin-password': adminPassword() })
