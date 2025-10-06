export default function ReneAILanding() {
  const API_BASE_URL = "http://54.196.196.126:5000";

  const handleTestCall = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/call`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ test: true }),
      });
      if (response.ok) {
        alert("✅ Test call initiated! Check your phone.");
      } else {
        alert("⚠️ Something went wrong triggering the call.");
      }
    } catch (error) {
      alert("❌ Failed to reach the server. Check if your backend is running.");
    }
  };

  return (
    <div style={{ backgroundColor: "#0D0F14", color: "white", display: "flex", flexDirection: "column", alignItems: "center", padding: "0 1.5rem" }}>
      <header style={{ width: "100%", padding: "1rem 1.5rem", display: "flex", justifyContent: "center", borderBottom: "1px solid #222630", backgroundColor: "rgba(13, 15, 20, 0.9)", position: "sticky", top: 0, zIndex: 50, cursor: "pointer" }}
        onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
        <h2 style={{ fontSize: "1.5rem", fontWeight: "bold", color: "#2D9CFF" }}>ReneAI</h2>
      </header>

      <section style={{ textAlign: "center", padding: "8rem 0", maxWidth: "48rem" }}>
        <h1 style={{ fontSize: "3rem", fontWeight: "bold", marginBottom: "1rem" }}>
          Your 24/7 AI Receptionist — Book, Cancel, Reschedule, and Answer Calls Instantly.
        </h1>
        <p style={{ fontSize: "1.125rem", color: "#C5C8CE", marginBottom: "2rem" }}>
          ReneAI handles your front desk so you can focus on your business.
        </p>
        <div style={{ display: "flex", justifyContent: "center", gap: "1rem" }}>
          <button style={{ backgroundColor: "#2D9CFF", color: "white", padding: "0.75rem 1.5rem", borderRadius: "0.75rem", border: "none", fontWeight: 600 }}>
            Get Started
          </button>
          <button style={{ border: "1px solid #2D9CFF", color: "#2D9CFF", padding: "0.75rem 1.5rem", borderRadius: "0.75rem", background: "none", fontWeight: 600 }}>
            Book a Demo
          </button>
        </div>
      </section>

      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: "2rem", maxWidth: "72rem", padding: "6rem 0", borderTop: "1px solid #222630" }}>
        {[
          { title: "Automated scheduling.", desc: "ReneAI manages bookings, cancellations, and reschedules with ease." },
          { title: "Seamless reminders.", desc: "Sends confirmations, reminders, and outbound calls for follow-ups." },
          { title: "24/7 availability.", desc: "Natural speech, Google integration, and industry versatility built in." },
        ].map((f, i) => (
          <div key={i} style={{ background: "#171A22", border: "1px solid #222630", borderRadius: "1rem", padding: "2rem", textAlign: "left" }}>
            <h3 style={{ fontSize: "1.5rem", fontWeight: "600", marginBottom: "0.75rem", color: "white" }}>{f.title}</h3>
            <p style={{ color: "#C5C8CE", fontSize: "0.875rem", lineHeight: "1.5" }}>{f.desc}</p>
          </div>
        ))}
      </section>

      <section style={{ maxWidth: "48rem", textAlign: "center", padding: "6rem 0", borderTop: "1px solid #222630" }}>
        <h2 style={{ fontSize: "2.25rem", fontWeight: "bold", marginBottom: "1.5rem" }}>Experience ReneAI Live</h2>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", marginBottom: "2rem", lineHeight: "1.6" }}>
          Watch our demo or make a sample test call to see ReneAI in action. Enjoy a preview of smart, friendly responses and a seamless scheduling experience for your business. Discover how easy front-desk automation can be!
        </p>
        <button onClick={handleTestCall} style={{ display: "inline-block", backgroundColor: "#2D9CFF", color: "white", padding: "1rem 2rem", borderRadius: "0.75rem", fontWeight: 600, textDecoration: "none", border: "none", cursor: "pointer" }}>
          Make a Free Test Call Here
        </button>
      </section>

      <section style={{ maxWidth: "48rem", textAlign: "center", padding: "6rem 0", borderTop: "1px solid #222630" }}>
        <h2 style={{ fontSize: "2.25rem", fontWeight: "bold", marginBottom: "1.5rem" }}>Privacy & Terms</h2>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", lineHeight: "1.6", marginBottom: "1.5rem" }}>
          ReneAI respects your privacy. We do not collect or store personal information through this website. Phone calls are securely handled by Twilio, a trusted communications provider, and follow standard encryption and data-protection practices.
        </p>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", lineHeight: "1.6", marginBottom: "1.5rem" }}>
          By using ReneAI’s services, you agree that interactions may be processed for scheduling and support purposes only. No call content is shared or sold to third parties. We comply with applicable privacy and communications laws in Canada and internationally.
        </p>
        <p style={{ color: "#C5C8CE", fontSize: "1rem", lineHeight: "1.6" }}>
          For questions about privacy or data handling, contact us at{" "}
          <a href="mailto:support@reneai.com" style={{ color: "#2D9CFF", textDecoration: "none" }}>
            support@reneai.com
          </a>.
        </p>
      </section>

      <footer style={{ color: "#8B90A0", fontSize: "0.875rem", padding: "4rem 0", borderTop: "1px solid #222630", width: "100%", maxWidth: "72rem", textAlign: "center" }}>
        <p>support@reneai.com</p>
        <p style={{ marginTop: "0.5rem" }}>© {new Date().getFullYear()} ReneAI — All rights reserved.</p>
      </footer>
    </div>
  );
}
