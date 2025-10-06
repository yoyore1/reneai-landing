import Navbar from "./Navbar";
import Footer from "./Footer";

export default function Contact() {
  return (
    <div style={{
      color: "white",
      backgroundColor: "rgba(13,15,20,0.9)",
      backdropFilter: "blur(5px)",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      padding: "0 1.5rem",
    }}>
      <Navbar />
      <section style={{ textAlign: "center", padding: "6rem 0", maxWidth: "48rem" }}>
        <h1 style={{ fontSize: "2.5rem", fontWeight: "bold", marginBottom: "1rem" }}>Contact Us</h1>
        <p style={{ color: "#C5C8CE", fontSize: "1.125rem", marginBottom: "3rem" }}>
          Have questions or want to book a demo? Reach us anytime.
        </p>
        <form style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
          <input type="text" placeholder="Your Name" style={{ padding: "0.75rem", borderRadius: "0.5rem", border: "none", backgroundColor: "#171A22", color: "white" }} />
          <input type="email" placeholder="Your Email" style={{ padding: "0.75rem", borderRadius: "0.5rem", border: "none", backgroundColor: "#171A22", color: "white" }} />
          <textarea placeholder="Message" rows="5" style={{ padding: "0.75rem", borderRadius: "0.5rem", border: "none", backgroundColor: "#171A22", color: "white" }} />
          <button style={{ backgroundColor: "#2D9CFF", color: "white", padding: "0.75rem", borderRadius: "0.5rem", border: "none", fontWeight: 600 }}>
            Send Message
          </button>
        </form>
        <p style={{ color: "#C5C8CE", marginTop: "2rem" }}>📧 support@reneai.com</p>
      </section>
      <Footer />
    </div>
  );
}
