interface SignupData {
  name?: string;
  email?: string;
  plan?: string;
  pain_point?: string;
  timestamp?: string;
  referrer?: string;
}

interface Env {
  SIGNUPS: KVNamespace;
}

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function jsonResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders },
  });
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export const onRequestPost: PagesFunction<Env> = async (context) => {
  try {
    const data: SignupData = await context.request.json();

    const email = data.email?.trim().toLowerCase();
    if (!email) {
      return jsonResponse({ success: false, error: "Email is required" }, 400);
    }
    if (!EMAIL_RE.test(email)) {
      return jsonResponse({ success: false, error: "Invalid email format" }, 400);
    }

    const name = data.name?.trim() || "";
    const plan = ["free", "pro", "team"].includes(data.plan || "") ? data.plan : "pro";

    const record = {
      email,
      name,
      plan,
      pain_point: data.pain_point?.trim() || "",
      referrer: data.referrer || "direct",
      source: "landing_page",
      timestamp: data.timestamp || new Date().toISOString(),
    };

    await context.env.SIGNUPS.put(email, JSON.stringify(record));

    return jsonResponse({ success: true, message: "You're on the list!" });
  } catch {
    return jsonResponse({ success: false, error: "Invalid request" }, 400);
  }
};

export const onRequestOptions: PagesFunction<Env> = async () => {
  return new Response(null, { headers: corsHeaders });
};
