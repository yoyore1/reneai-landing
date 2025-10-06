import React from "react";

export default function Footer() {
  return (
    <footer style={{
      color: "#8B90A0",
      fontSize: "0.875rem",
      padding: "4rem 0",
      borderTop: "1px solid rgba(255,255,255,0.1)",
      width: "100%",
      maxWidth: "72rem",
      textAlign: "center",
      marginTop: "4rem",
    }}>
      <p>support@reneai.com</p>
      <p style={{ marginTop: "0.5rem" }}>
        © {new Date().getFullYear()} ReneAI — All rights reserved.
      </p>
    </footer>
  );
}
