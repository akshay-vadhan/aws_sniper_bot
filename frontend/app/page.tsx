import Dashboard from "./ui";

const api = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function getDashboard() {
  try {
    const response = await fetch(`${api}/api/dashboard`, { cache: "no-store" });
    if (!response.ok) throw new Error("API unavailable");
    return await response.json();
  } catch {
    return null;
  }
}

export default async function Home() {
  const data = await getDashboard();
  return <Dashboard initialData={data} />;
}
