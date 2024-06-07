import { Example } from "./Example";

import styles from "./Example.module.css";

const DEFAULT_EXAMPLES: string[] = [
    "Why is self-awareness important in identifying one’s true purpose and passion?",
    "How does the speaker differentiate between purpose and passion?",
    "How do you think adversity shapes one's sense of purpose, drawing from Viktor Frankl's experience?"
];

const GPT4V_EXAMPLES: string[] = [
    "What is the shift in understanding of purpose among young adults?",
    "Compare the role of external influences and internal introspection in shaping one's purpose and passion.",
    "Can you identify any relation between engaging in introspective practices (like journaling) and achieving a deeper sense of self and purpose?"
];

interface Props {
    onExampleClicked: (value: string) => void;
    useGPT4V?: boolean;
}

export const ExampleList = ({ onExampleClicked, useGPT4V }: Props) => {
    return (
        <ul className={styles.examplesNavList}>
            {(useGPT4V ? GPT4V_EXAMPLES : DEFAULT_EXAMPLES).map((question, i) => (
                <li key={i}>
                    <Example text={question} value={question} onClick={onExampleClicked} />
                </li>
            ))}
        </ul>
    );
};
