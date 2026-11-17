import React from "react";
import type { TableViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface TableViewProps {
  data: TableViewData;
}

export const TableView: React.FC<TableViewProps> = ({ data }) => {
  const fg = componentFg(data);
  const shown = data.rows.length;
  const truncated =
    data.total_rows != null &&
    data.total_rows > shown &&
    data.reference != null;
  return (
    <div
      data-testid="table-view"
      style={componentStyle(data)}
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm"
    >
      <div className="overflow-x-auto">
        <table className="border-collapse text-xs font-mono">
          <thead>
            <tr>
              {data.columns.map((column, index) => (
                <th
                  key={index}
                  style={fg}
                  className="border border-neutral-700 px-2 py-1 text-left font-semibold text-neutral-300"
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    style={fg}
                    className="border border-neutral-800 px-2 py-1 text-neutral-200"
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && data.reference && (
        <div className="text-xs text-neutral-400 mt-2 font-mono">
          [showing {shown}/{data.total_rows} rows; full table: artifact://
          {data.reference.sha256}]
        </div>
      )}
    </div>
  );
};
